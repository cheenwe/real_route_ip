# real_route_ip

基于 FastAPI 的获取路由器真实公网 IP 的服务。

解决痛点: 家庭宽带使用路由器拨号上网，实际拨号获得的公网ip和和真实IP不一致的问题，并支持DDNS-GO调用。

本项目基于[京东云无线宝]路由器开发，其他路由器原理相同（自动登录->找到拨号上网查看ip）。

## 功能

- 登录路由器获取 `ubus_rpc_session`
- 调用 `get_wan_info` 获取 WAN 信息并解析真实 IP
- 自动后台刷新真实 IP，并写入本地 `ip_cache.json`
- 默认接口优先读取缓存 JSON，减少对路由器请求
- 自动刷新间隔支持在页面按“分钟”配置
- 对外提供接口：`GET /api/ip` 返回 `{ "ip": "x.x.x.x" }`
- 所有参数支持在线配置（接口 + Web 页面）
- 支持调试模式（`GET /api/ip?debug=true`）
- 支持 Docker 部署

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp  runtime_config.json.sample runtime_config.json
python main.py
```

或使用 uvicorn 命令：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

打开浏览器访问：

- Web 页面: `http://<你的机器IP>:8000/`
- 配置接口: `http://127.0.0.1:8000/api/config`
- IP 接口: `http://127.0.0.1:8000/api/ip`

## Docker 运行

```bash
docker build -t real-route-ip .
docker run --rm -p 8000:8000 real-route-ip
```

## 主要接口

- `GET /api/config`：查看当前配置
- `PUT /api/config`：更新配置
- `POST /api/config/reset`：重置默认配置
- `GET /api/ip`：返回真实 IP
- `GET /api/ip?debug=true`：实时请求路由器，返回真实 IP + 调试信息，并更新缓存
- `GET /api/ddns-go/ip`：ddns-go 专用，纯文本返回 IP
- `GET /api/ip/ddns-go`：同上（兼容别名）
- `GET /api/ip/cache`：查看当前缓存 JSON 数据
- `GET /api/health`：健康检查
