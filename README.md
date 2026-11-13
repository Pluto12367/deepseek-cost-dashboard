# DeepSeek 成本监控面板(deepseek-cost-dashboard)

DeepSeek「全家桶」API 把计费从单一 token 变成了**四个维度**:

| 维度 | 计费方式 |
|---|---|
| 模型 token | 元/百万 tokens,分「缓存命中/未命中 × 高峰/空闲」四档 |
| 联网搜索 / 图片搜索 / 网页抓取 | 元/千次 |
| OCR | 元/千页 |
| ASR / TTS | 元/小时、元/万字符 |

而官方**只提供余额查询接口(`/user/balance`),没有用量统计 API**——开发者无法知道钱花在哪,也难以设预算告警。

本项目在本地起一个**记账反向代理**:应用的 `base_url` 指向它,它透传请求到 `api.deepseek.com` 的同时解析每次响应的 `usage`(含 `server_tool_use` 工具调用计数),按价格表精确估算每笔费用,存入 SQLite,并在 Web 面板可视化。

## 功能

- **开箱即用**:双击 `start.bat` / `./start.sh` 一键启动并自动打开浏览器;面板内嵌 Python/Node/curl 接入片段一键复制;无数据时显示三步引导
- **实时监控**:自动刷新(关/5s/30s,像 `tail -f`)、浏览器标签页标题常显今日花费、超预算桌面通知、新调用行闪烁
- **下钻筛选**:点击日志中的模型/Key/端点即自动筛选;点击图表某天柱子跳到那天明细;筛选变更即生效;错误徽章点击展开全文
- **概览卡片**:今日 / 本月 / 累计花费与调用次数、较昨日趋势、账户余额(可选配置 Key 轮询官方接口)
- **月预算告警**:进度条 + 80% 预警 / 100% 告警;代理响应头 `X-DSCD-Budget-Warning` 同步提示;**按近 7 天日均推算月底预计花费,超预算提前亮红**
- **Webhook 告警**:月花费达 80% / 100% 时推送通知,**原生适配钉钉 / 飞书 / 企业微信机器人格式**(也可选通用 JSON),同一级别每月只推一次,面板一键测试
- **Key 分组统计**:代理从 `Authorization` 自动识别调用方 Key,按 Key 统计费用/调用数/占比;label 可自定义;只保存哈希与掩码,**不落盘完整 Key**
- **流式 usage 注入**:自动为流式 `/chat/completions` 请求注入 `stream_options.include_usage`,让流式调用的 token 用量也能精确统计(可在设置中关闭;客户端显式关闭时不覆盖)
- **图表**:每日花费堆叠柱图(按类别)、费用构成环形图(中心显示总额)、模型花费对比
- **调用日志**:每次调用的端点、模型、Key、token 明细、搜索/抓取/OCR/ASR/TTS 用量、费用明细(只显示非零项),支持按 Key/模型/端点/某天/来源筛选、翻页、**CSV 导出**;单笔高价调用自动标红 ⚠
- **数据管理**:价格表在线编辑(写回 `prices.json` 立即生效)、数据保留期自动清理
- **三协议兼容**:Chat Completions / Responses / Messages 的 usage 字段自动归一化;**SSE 流式透传**并在流结束后解析记账
- **演示模式**:一键生成 30 天模拟数据,零费用、无需 API Key 即可演示

## 快速开始

Windows 双击 `start.bat`(或 `./start.sh`),首次运行自动装依赖、启动后自动打开浏览器;也可以手动:

```bash
pip install -r requirements.txt
python demo_seed.py        # 可选:灌入演示数据
python main.py             # 启动
```

打开面板:<http://127.0.0.1:8787/ui>(设 `DSCD_NO_BROWSER=1` 可禁用自动开浏览器)

## 接入你的应用

把 DeepSeek SDK/HTTP 客户端的 `base_url` 指向本代理,API Key 照常传(代理原样透传):

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",                       # 真实 Key,代理透传
    base_url="http://127.0.0.1:8787",       # 指向本地代理
)
resp = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "帮我搜下今天的AI新闻"}],
    tools=[{"type": "deepseek_web_search"}],
)
print(resp.headers)  # 非流式响应可从 X-DSCD-Cost-Total 读到本次估算费用
```

curl 直连代理也一样:

```bash
curl http://127.0.0.1:8787/chat/completions \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"你好"}]}'
```

## 计费口径与已知局限

- 所有费用为**估算**(官方刊例价 × usage 计数),用于监控与预算,记账以官方账单为准
- 高峰时段按北京时间周一至周五 9:00–12:00、14:00–18:00 判断,法定节假日未剔除(可在 `prices.json` 调整)
- 流式请求默认由代理注入 `stream_options.include_usage`(设置中可关);若客户端显式关闭该选项,Chat Completions 的 token 用量可能统计不到(工具调用次数不受影响)
- OCR / ASR / TTS 独立接口的响应字段官方未完全公开,面板采用**防御性解析**:`prices.json` 中 `standalone.*.*_fields` 可自行配置字段映射,解析不到时按请求参数兜底(OCR 默认按 1 页计)
- 未知模型的 token 单价回落到 `default_model`(默认 deepseek-flash)

## 目录结构

```
main.py        FastAPI 入口:REST API + 静态面板 + 兜底代理路由
proxy.py       反向代理:透传、SSE 流式、include_usage 注入、Key 识别、记账
estimator.py   计费引擎:usage 归一化、高峰时段、单价计算
db.py          SQLite:calls 日志表、keys 分组表、settings(预算/Key/Webhook)
notify.py      Webhook 预算告警(阈值判断、去重、推送)
prices.json    可编辑价格表(默认与官方刊例对齐)
demo_seed.py   30 天模拟数据生成器(--wipe 清除)
static/        面板前端(Chart.js,优先本地 vendor,缺失时回落 CDN)
tests/         mock 上游与 Webhook 接收器(零费用端到端测试)
```

## Key 分组与隐私

- 全部数据存在本机 `data.db`,不出本机
- 代理从每次请求的 `Authorization` 头识别调用方 Key,**只保存 SHA-256 哈希前缀与 `sk-***尾4位` 掩码**,完整 Key 不落盘;面板可为每个 Key 设置 label,统计与筛选全链路生效
- 面板「设置」中保存的 API Key 仅用于轮询 `/user/balance`,请勿在公共服务上部署本面板;如需共享部署,请自行增加鉴权

## Webhook 告警

在面板「设置」中配置 URL 后,月花费达到预算 80% / 100% 时会 POST 如下 JSON:

```json
{
  "event": "budget_exceeded",
  "month": "2026-11",
  "month_cost": 38.75,
  "budget": 10.0,
  "percent": 387.6,
  "source": "deepseek-cost-dashboard",
  "ts": "2026-11-14 00:10:37"
}
```

同一级别每月只推送一次,花费回落到 80% 以下自动复位。可用于对接企业微信/钉钉/飞书群机器人或自建服务。

## 开发与测试

上游地址可用环境变量覆盖,配合本地 mock 即可**零费用**跑通端到端流程(注入、流式记账、Key 分组、Webhook):

```bash
python tests/mock_upstream.py upstream   # 127.0.0.1:9999,模拟 api.deepseek.com
python tests/mock_upstream.py webhook    # 127.0.0.1:9998,接收告警 POST
DEEPSEEK_UPSTREAM=http://127.0.0.1:9999 python main.py
```

## Roadmap

- [x] 流式请求自动注入 `stream_options.include_usage`(可开关)
- [x] 多 API Key 分组统计
- [x] Webhook 预算告警
- [ ] 邮件/IM 机器人通知模板
- [ ] 官方用量 API 上线后的自动适配

## License

MIT
