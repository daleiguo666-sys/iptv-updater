# iptv-updater

聚合公开 IPTV 源 → 探活去死链 → 输出单一 `live.m3u`，GitHub Actions 每 6 小时自动刷新。
体育频道（CCTV5/5+、广东体育、咪咕、ESPN 等）置顶分组，方便看 NBA。

## 订阅

推 GitHub 之后，播放器（IINA / VLC / PotPlayer / TiviMate / Surge TV）订阅：

```
https://raw.githubusercontent.com/<your-user>/iptv-updater/main/live.m3u
```

## 本地跑

```bash
pip install -r requirements.txt
python update.py
```

产物在 `./live.m3u`。

## 维护

加/删上游源、调体育关键词，都改 `sources.yaml` 一个文件。

| 字段 | 说明 |
|---|---|
| `sources` | 上游 m3u URL 列表，按顺序拉取并合并 |
| `sports_keywords` | 频道名/分组命中即归入"体育"组并置顶；顺序决定置顶优先级 |
| `blacklist_domains` | 直接跳过的死链域名，省探活时间 |
| `probe.timeout_seconds` | 单源探活超时（默认 3s） |
| `probe.workers` | 并发探活线程数（默认 64） |
| `probe.read_bytes` | 探活时读多少字节算"首帧可达"（默认 4096） |

## 探活逻辑

- HTTP/HTTPS：`GET` 流式拉首 4KB，3s 超时，超时/4xx/5xx 视为死链丢弃
- `udp://` / `rtp://` / `rtmp://` 直接保留（HTTP 探不动，留给播放器自己试），但排序靠后
- 排序：体育关键词命中置顶，其余按延迟升序

## 触发

- 每 6 小时自动跑一次（GitHub Actions cron）
- Actions 页面手动 `Run workflow` 也可立刻刷新

## 备注

- 海外源（ESPN/NBA TV 等）多数有地域限制，国内播放器不一定能看，CCTV5/5+/咪咕通常可用
- `live.m3u` 由 Actions 自动覆盖，本地不要手改
