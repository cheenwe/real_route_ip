import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field


CONFIG_FILE = Path(__file__).parent / "runtime_config.json"
IP_CACHE_FILE = Path(__file__).parent / "ip_cache.json"
DEFAULT_SESSION_TOKEN = "00000000000000000000000000000000"


class RouterConfig(BaseModel):
    router_base_url: str = Field(default="http://192.168.68.1")
    api_path: str = Field(default="/jdcapi")
    username: str = Field(default="root")
    password: str = Field(default="1008610086")
    session_timeout: int = Field(default=600)
    request_timeout: float = Field(default=10.0)
    verify_ssl: bool = Field(default=False)
    login_service: str = Field(default="session")
    login_method: str = Field(default="login")
    wan_service: str = Field(default="jdcapi.static")
    wan_method: str = Field(default="get_wan_info")
    ip_path: str = Field(default="pppoe_info.ipaddr")
    auto_refresh_enabled: bool = Field(default=True)
    auto_refresh_interval_minutes: int = Field(default=1)
    headers: Dict[str, str] = Field(default_factory=dict)
    cookies: Dict[str, str] = Field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return urljoin(self.router_base_url.rstrip("/") + "/", self.api_path.lstrip("/"))


class ConfigStore:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> RouterConfig:
        if not self.file_path.exists():
            config = RouterConfig()
            self._save(config)
            return config
        data = json.loads(self.file_path.read_text(encoding="utf-8"))
        # 兼容旧配置：auto_refresh_interval_seconds -> auto_refresh_interval_minutes
        if (
            "auto_refresh_interval_minutes" not in data
            and "auto_refresh_interval_seconds" in data
        ):
            seconds = int(data.get("auto_refresh_interval_seconds") or 60)
            data["auto_refresh_interval_minutes"] = max(1, (seconds + 59) // 60)
        return RouterConfig(**data)

    def _save(self, config: RouterConfig) -> None:
        self.file_path.write_text(
            json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self) -> RouterConfig:
        with self._lock:
            return RouterConfig(**self._config.model_dump())

    def update(self, config: RouterConfig) -> RouterConfig:
        with self._lock:
            self._config = config
            self._save(config)
            return RouterConfig(**self._config.model_dump())

    def reset(self) -> RouterConfig:
        with self._lock:
            self._config = RouterConfig()
            self._save(self._config)
            return RouterConfig(**self._config.model_dump())


class RouterClientError(Exception):
    pass


class IpCache(BaseModel):
    ip: Optional[str] = None
    updated_at: Optional[int] = None
    last_checked_at: Optional[int] = None
    source: str = "none"


class IpCacheStore:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._lock = threading.Lock()
        self._cache = self._load()

    def _load(self) -> IpCache:
        if not self.file_path.exists():
            cache = IpCache()
            self._save(cache)
            return cache
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            return IpCache(**data)
        except (json.JSONDecodeError, ValueError):
            cache = IpCache()
            self._save(cache)
            return cache

    def _save(self, cache: IpCache) -> None:
        self.file_path.write_text(
            json.dumps(cache.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self) -> IpCache:
        with self._lock:
            return IpCache(**self._cache.model_dump())

    def update_ip(self, ip: str, source: str) -> bool:
        with self._lock:
            now = int(time.time())
            changed = ip != self._cache.ip
            self._cache.last_checked_at = now
            self._cache.source = source
            if changed:
                self._cache.ip = ip
                self._cache.updated_at = now
            self._save(self._cache)
            return changed

    def mark_checked(self, source: str) -> None:
        with self._lock:
            self._cache.last_checked_at = int(time.time())
            self._cache.source = source
            self._save(self._cache)


def _build_call_payload(
    session_id: str, service: str, method: str, params: Dict[str, Any], rpc_id: int
) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "call",
        "params": [session_id, service, method, params],
    }


def _extract_ip(wan_info: Dict[str, Any], ip_path: str) -> str:
    current: Any = wan_info
    for key in ip_path.split("."):
        if not isinstance(current, dict) or key not in current:
            raise RouterClientError(f"无法通过 ip_path={ip_path} 解析 IP 地址")
        current = current[key]
    if not isinstance(current, str) or not current:
        raise RouterClientError("路由器返回的 IP 字段为空或类型不正确")
    return current


def _call_router_jsonrpc(
    session: requests.Session,
    endpoint: str,
    payload: Dict[str, Any],
    timeout: float,
    verify_ssl: bool,
) -> Dict[str, Any]:
    response = session.post(
        endpoint,
        json=payload,
        timeout=timeout,
        verify=verify_ssl,
    )
    response.raise_for_status()
    return response.json()


def fetch_real_ip(config: RouterConfig, debug: bool = False) -> Tuple[str, Dict[str, Any]]:
    session = requests.Session()
    session.headers.update(config.headers)
    session.cookies.update(config.cookies)
    endpoint = config.endpoint

    login_payload = _build_call_payload(
        session_id=DEFAULT_SESSION_TOKEN,
        service=config.login_service,
        method=config.login_method,
        params={
            "username": config.username,
            "password": config.password,
            "timeout": config.session_timeout,
        },
        rpc_id=1,
    )

    debug_data: Dict[str, Any] = {"endpoint": endpoint}
    start_ts = time.time()

    try:
        login_resp = _call_router_jsonrpc(
            session=session,
            endpoint=endpoint,
            payload=login_payload,
            timeout=config.request_timeout,
            verify_ssl=config.verify_ssl,
        )
    except requests.RequestException as exc:
        raise RouterClientError(f"登录请求失败: {exc}") from exc

    try:
        ubus_session = login_resp["result"][1]["ubus_rpc_session"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RouterClientError("登录成功但未找到 ubus_rpc_session") from exc

    wan_payload = _build_call_payload(
        session_id=ubus_session,
        service=config.wan_service,
        method=config.wan_method,
        params={},
        rpc_id=2,
    )

    try:
        wan_resp = _call_router_jsonrpc(
            session=session,
            endpoint=endpoint,
            payload=wan_payload,
            timeout=config.request_timeout,
            verify_ssl=config.verify_ssl,
        )
    except requests.RequestException as exc:
        raise RouterClientError(f"WAN 信息请求失败: {exc}") from exc

    try:
        wan_info = wan_resp["result"][1]
    except (KeyError, IndexError, TypeError) as exc:
        raise RouterClientError("WAN 信息响应结构不符合预期") from exc

    ip_address = _extract_ip(wan_info, config.ip_path)
    debug_data["elapsed_ms"] = int((time.time() - start_ts) * 1000)

    if debug:
        debug_data["login_payload"] = login_payload
        debug_data["login_response"] = login_resp
        debug_data["wan_payload"] = wan_payload
        debug_data["wan_response"] = wan_resp
        debug_data["session_id"] = ubus_session

    return ip_address, debug_data


def refresh_ip_and_persist(
    config: RouterConfig,
    cache_store: IpCacheStore,
    source: str,
    debug: bool = False,
) -> Tuple[str, Dict[str, Any], bool]:
    ip, debug_data = fetch_real_ip(config=config, debug=debug)
    changed = cache_store.update_ip(ip=ip, source=source)
    return ip, debug_data, changed


class AutoRefreshWorker:
    def __init__(self, config_store: ConfigStore, cache_store: IpCacheStore):
        self.config_store = config_store
        self.cache_store = cache_store
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            config = self.config_store.get()
            interval = max(60, int(config.auto_refresh_interval_minutes or 1) * 60)
            if not config.auto_refresh_enabled:
                self._stop_event.wait(timeout=min(interval, 10))
                continue
            try:
                refresh_ip_and_persist(
                    config=config,
                    cache_store=self.cache_store,
                    source="auto_refresh",
                    debug=False,
                )
            except RouterClientError:
                self.cache_store.mark_checked(source="auto_refresh_error")
            self._stop_event.wait(timeout=interval)


def get_ip_from_cache_or_router(config: RouterConfig) -> Tuple[str, str, IpCache]:
    cache = ip_cache_store.get()
    if cache.ip:
        return cache.ip, "cache", cache
    ip, _, _ = refresh_ip_and_persist(
        config=config, cache_store=ip_cache_store, source="cache_miss", debug=False
    )
    return ip, "router", ip_cache_store.get()


app = FastAPI(title="Router Real IP Service", version="1.0.0")
config_store = ConfigStore(CONFIG_FILE)
ip_cache_store = IpCacheStore(IP_CACHE_FILE)
auto_refresh_worker = AutoRefreshWorker(config_store=config_store, cache_store=ip_cache_store)


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    config = config_store.get()
    return config.model_dump()


@app.put("/api/config")
def update_config(config: RouterConfig) -> Dict[str, Any]:
    updated = config_store.update(config)
    return {"message": "配置已更新", "config": updated.model_dump()}


@app.post("/api/config/reset")
def reset_config() -> Dict[str, Any]:
    reseted = config_store.reset()
    return {"message": "配置已重置", "config": reseted.model_dump()}


@app.get("/api/ip")
def get_real_ip(debug: bool = Query(default=False)) -> Dict[str, Any]:
    config = config_store.get()
    try:
        if debug:
            ip, debug_data, changed = refresh_ip_and_persist(
                config=config, cache_store=ip_cache_store, source="debug", debug=True
            )
            cache = ip_cache_store.get()
            return {
                "ip": ip,
                "source": "router_debug",
                "cache_updated": changed,
                "cache": cache.model_dump(),
                "debug": debug_data,
            }
        ip, source, cache = get_ip_from_cache_or_router(config=config)
    except RouterClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ip": ip, "source": source, "cache": cache.model_dump()}


@app.get("/api/ddns-go/ip", response_class=PlainTextResponse)
@app.get("/api/ip/ddns-go", response_class=PlainTextResponse)
def get_real_ip_for_ddns_go() -> PlainTextResponse:
    config = config_store.get()
    try:
        ip, _, _ = get_ip_from_cache_or_router(config=config)
    except RouterClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # ddns-go 从返回文本中正则提取 IP，纯文本兼容性最好。
    return PlainTextResponse(content=ip)


@app.get("/api/ip/cache")
def get_ip_cache() -> Dict[str, Any]:
    return ip_cache_store.get().model_dump()


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.on_event("startup")
def startup_event() -> None:
    auto_refresh_worker.start()


@app.on_event("shutdown")
def shutdown_event() -> None:
    auto_refresh_worker.stop()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
