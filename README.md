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

- **概览卡片**:今日 / 本月 / 累计花费与调用次数,账户余额(可选配置 Key 轮询官方接口)
- **月预算告警**:进度条 + 80% 预警 / 100% 告警;代理响应头 `X-DSCD-Budget-Warning` 同步提示
- **图表**:每日花费堆叠柱图(按类别)、费用构成饼图、模型花费对比
- **调用日志**:每次调用的端点、模型、token 明细、搜索/抓取/OCR/ASR/TTS 用量、费用明细,支持筛选、翻页、**CSV 导出**
- **价格表在线编辑**:面板上直接改单价,写回 `prices.json` 立即生效
- **三协议兼容**:Chat Completions / Responses / Messages 的 usage 字段自动归一化;**SSE 流式透传**并在流结束后解析记账
- **演示模式**:一键生成 30 天模拟数据,零费用、无需 API Key 即可演示

## 快速开始

```bash
pip install -r requirements.txt
python demo_seed.py        # 可选:灌入演示数据
python main.py             # 启动
```

打开面板:<http://127.0.0.1:8787/ui>

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
- 流式请求若客户端未开启 `stream_options.include_usage`,Chat Completions 的 token 用量可能统计不到(工具调用次数不受影响);建议接入方开启该选项
- OCR / ASR / TTS 独立接口的响应字段官方未完全公开,面板采用**防御性解析**:`prices.json` 中 `standalone.*.*_fields` 可自行配置字段映射,解析不到时按请求参数兜底(OCR 默认按 1 页计)
- 未知模型的 token 单价回落到 `default_model`(默认 deepseek-flash)

## 目录结构

```
main.py        FastAPI 入口:REST API + 静态面板 + 兜底代理路由
proxy.py       反向代理:透传、SSE 流式、记账
estimator.py   计费引擎:usage 归一化、高峰时段、单价计算
db.py          SQLite:calls 日志表 + settings(预算/Key)
prices.json    可编辑价格表(默认与官方刊例对齐)
demo_seed.py   30 天模拟数据生成器(--wipe 清除)
static/        面板前端(Chart.js,优先本地 vendor,缺失时回落 CDN)
```

## 数据与隐私

- 全部数据存在本机 `data.db`,不出本机
- 面板中保存的 API Key 仅用于轮询 `/user/balance`,请勿在公共服务上部署本面板;如需共享部署,请自行增加鉴权

## Roadmap

- [ ] 流式请求自动注入 `stream_options.include_usage`(可开关)
- [ ] 多 API Key 分组统计
- [ ] Webhook / 邮件预算告警
- [ ] 官方用量 API 上线后的自动适配

## License

MIT
