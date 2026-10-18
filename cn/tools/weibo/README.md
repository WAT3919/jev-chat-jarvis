# 微博帖子分析工具 (tools/weibo/)

本地命令行工具:输入一条微博帖子(粘贴正文 / 文本文件 / 帖子链接自动抓取),用与 Jev 聊天助手同一套判断管线分析它,并给出 3 条候选回复(评论)。

- **判断**:Jev decisions 接口(`POST /alpha/decisions`,默认 `typesafe/jev-1.13`),7 个维度:发布者意图 / 内容风险(0–9)/ 事实可信度 / 攻击性(0–9)/ 该不该回 / 最佳动作 / 对方要什么。题目用英文写,帖子原文保留中文(与 `tools/jev/` 口径一致)。
- **候选回复**:OpenAI 兼容接口(默认 `deepseek/deepseek-chat-v3.1`)起草 3 条候选评论,再由 Jev 按「最合适」排序并给出占比。
- **不自动发任何东西**:工具只输出分析和候选文本,发不发由你。

## 快速开始

```bash
pip install requests   # 唯一依赖,Python 3.9+

# 1. 配置(把 config.example.json 复制一份改名,填密钥/Cookie;文件已 gitignore)
cp tools/weibo/config.example.json tools/weibo/config.json

# 2a. 直接粘贴帖子正文
python tools/weibo/analyze.py --text "今天遇到了一个离谱的事..." --author "某博主"

# 2b. 从链接抓取
python tools/weibo/analyze.py --url "https://weibo.com/5979979070/AbCdEf123456"

# 2c. 文件 / 交互式
python tools/weibo/analyze.py --file posts.txt
python tools/weibo/analyze.py
```

无 Cookie 时工具会走 `m.weibo.cn/statuses/show` 游客接口兜底(不少公开帖子无需登录可读);遇到「接口返回异常」或被风控,再配 Cookie。

## 配置(config.json)

```json
{
  "judge": {"base_url": "https://openrouter.ai/api",
            "model": "typesafe/jev-1.13", "api_key": "sk-or-..."},
  "reply": {"base_url": "https://openrouter.ai/api/v1",
            "model": "deepseek/deepseek-chat-v3.1", "api_key": ""},
  "weibo_cookie": "SCF=...; SUB=...; SUBP=...;"
}
```

- 判断/回复两路各自可配;`reply.api_key` 留空自动继承判断接口的密钥。
- `api_key` 也可不写文件,改用环境变量 `OPENROUTER_API_KEY`;Cookie 可用环境变量 `WEIBO_COOKIE`。
- **两种判断后端**:默认 `judge.protocol = "auto"`,按 `base_url` 自动选——域名含 openrouter/bocha/typesafe/vercel/opencode 走 **Jev 决策协议**(`/alpha/decisions` 或 `/v1/systemone`,质量最高);其它域名(如 DeepSeek 官方)走 **聊天模型判断兜底**(`chat_judge.py`,把题目连同答案格式发给 chat/completions,单轮作答 7 题 + 排序题,解析后与 Jev 同构)。也可显式设 `"protocol": "jev" | "chat"`。
- 密钥和 Cookie **不落日志、不出现在输出里**,打印前一律打码。

### 只用一把 DeepSeek 官方密钥跑通全链路

```json
{
  "judge": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "protocol": "auto", "api_key": "sk-你的DeepSeek密钥"},
  "reply": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "api_key": ""},
  "weibo_cookie": "SCF=...; SUB=...; SUBP=...;"
}
```

判断与回复共用这一把密钥(回复留空自动继承)。针对 DeepSeek 官方接口,工具会自动:关闭默认思考模式(`thinking=disabled`)、开启 JSON 强制输出(`response_format=json_object`)、限制 `max_tokens`——判断/草拟更快更省。模型名按官方现名填 `deepseek-flash`(旧名 `deepseek-chat`/`deepseek-v4-flash` 是别名,仍可用);想要更强草拟质量可把两路 `model` 换成 `deepseek-v4-pro` 实测对比。

聊天模型判断精度比专用 Jev 模型略低,日后拿到 OpenRouter(`sk-or-v1-` 开头)或博查等密钥,把 `judge.base_url/model/api_key` 换过去即切回 Jev 专用判断(面板的「判断」行会显示当前用的是哪种)。

### 判断接口换成博查 Jev(免费档位)

博查 Jev 与 TypeSafe 同协议,`base_url` 含 `bocha` 会自动走 `POST /v1/systemone`(与实测一致;密钥与额度申请见 [博查 Jev 文档](https://bocha-ai.feishu.cn/wiki/PhrPwCrEaiCyyPkkDNecdfRSnHR),需登录飞书):

```json
"judge": {"base_url": "https://jev.bocha.cn", "model": "bocha-jev-v1",
          "protocol": "auto", "api_key": "博查平台申请的key"}
```

回复路由保持 DeepSeek 不变即可(两路互不影响)。实测:博查 Jev 判一次约 0.45s、全链路约 2s,比聊天模型判断快一截;但两道样例上维度判断偏「雾」(如把无来源谣言标为「有据可查」、建议「认同支持」)——工具已加护栏:**只要判断认为帖子有风险(传播未证实/带节奏/引战、可信度存疑、风险≥4),候选回复自动切换为「求证质疑」策略,绝不生成认同、转发类回复**。两种判断后端一行配置即可切换:

```json
"judge": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
          "protocol": "auto", "api_key": "你的DeepSeek密钥"}
```

### 抓取接口

- 主用 `weibo.com/ajax/statuses/show?id={bid}`(带你的 Cookie,参考 weibo_scraper.py 的请求头配方);无 Cookie 自动走 `m.weibo.cn/statuses/show` 游客兜底(部分公开帖子可读)。

Cookie 获取:浏览器登录 weibo.com → F12 → Network → 刷新任意页面 → 找任一 `weibo.com` 请求 → 复制完整 `Cookie` 请求头。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--text / --file / --url` | 三种输入方式(URL 可以是 weibo.com、m.weibo.cn 链接或裸帖子 ID) |
| `--author / --context` | 粘贴输入时补充作者昵称、额外上下文(如原帖) |
| `--comments N\|all\|pick` | 拉取帖子评论并逐条分析「该怎么回」;**自动剔除楼主自己的评论**、覆盖一级评论与二级回复;`all`=全部分析、N=前 N 条、不带值=列表挑选。需配合 `--url` |
| `--as-poster` | 配合 `--comments`:候选回复以**原帖作者口吻**编写(自己回复自己帖子下的评论时用) |
| `--judge auto\|bocha\|ds` | 临时切换判断后端:`auto`=按配置,`bocha`=博查 Jev,`ds`=DeepSeek 聊天判断(用 reply 的密钥),对 config.json 无副作用 |
| `--payload` | 只打印将发送的 state+questions JSON,不调 API(免费调试题目集) |
| `--dump` | 只抓取并打印帖子原始 JSON(免费验证 Cookie 是否有效) |
| `--json` | 输出完整结构化结果(含原始 answers/latency/cost;评论区模式下是汇总数组) |

### 两人互动时间线(interactions.py)

```bash
# A 的帖子下,A、B 两人最近的互动,按时间排序,最多 100 条
python tools/weibo/interactions.py 5979979070 7945888606
python tools/weibo/interactions.py https://weibo.com/u/5979979070 "@单曲Kioku-午前4時" --pages 5 --limit 50
python tools/weibo/interactions.py A B --json   # 结构化输出
```

判定口径:**B 在 A 帖子下的任何评论 + A 在「含 B 评论」的楼里的回复**;扫描 A 的最近帖子(默认 10 页,跳过无评论帖,**默认跳过置顶帖**——置顶帖的互动往往很早,会把观察窗口拉长、让频率/时间衰减失真,`--include-top` 可关掉跳过),每帖评论楼按时间合并排序后取最新 N 条。用户标识**直接填昵称即可**(需已配置微博 Cookie;昵称经 s.weibo.com 用户搜索解析,优先精确匹配,搜不到精确项会取首条并在日志里警告),也支持 uid、`/u/` 链接、`/n/` 链接。注意这是批量读取,翻页自带随机休眠但仍比单帖分析重,大范围扫描请控制 `--pages`(详见 README 风控提示)。

### 图形界面(Web UI)

**启动方式(任选其一):**
- **双击 `tools/weibo/启动分析界面.bat`**(推荐;自动调 `py` 启动器,规避 cmd 里 `python` 指向 Microsoft Store 占位程序的问题)
- cmd / PowerShell:`py tools\weibo\webui.py`
- Git Bash / 依赖齐全的终端:`python tools/weibo/webui.py`
- 可选参数:`--port 9000 --no-browser`

启动后浏览器自动打开 `http://127.0.0.1:8770`(没打开就手动访问)。Web 部分是**纯标准库实现(http.server),无需安装 Flask**;整个工具的依赖只有 `requests`。

两个标签页:
1. **帖子/评论分析** — 贴链接或文本单帖分析;勾「分析评论区」拉全部层级评论、自动剔除楼主、逐条出卡片;「楼主口吻」「判断后端(自动/博查/DeepSeek)」即选。
2. **两人互动时间线** — 填用户 A、B(uid / `/u/` 链接 / 昵称)。**推荐流程:填昵称 → 点「搜索」→ 从候选列表(头像/昵称/uid/简介/认证标)里点选确认**;选定后输入框显示昵称、徽标显示「昵称 · uid 已选定」;与查询词完全一致的候选标「精确匹配」。扫描在后台任务中运行,页面每 2 秒轮询实时进度日志,完成后自动生成 **AI 关系总结**(关系类型、A→B/B→A 好感度进度条、依据、氛围、主要话题、分析与局限)并渲染时间线。点「**导出 PDF 报告**」可下载一份 A4 报告(含生成时间、双方信息、AI 总结与完整时间线);导出用系统 Chrome/Edge 无头渲染,找不到浏览器时会退化为打开可打印的报告页(浏览器里「另存为 PDF」效果相同)。

只监听本机 127.0.0.1,外部无法访问。

### 评论区分析示例

```bash
# 全量模式:拉取所有层级评论、剔除楼主自己的,其余逐条给候选回复(楼主口吻)
python tools/weibo/analyze.py --url "https://weibo.com/5979979070/5274255962150182" --comments all --as-poster

# 只分析前 5 条(按评论顺序,含二级回复)
python tools/weibo/analyze.py --url "<帖子链接>" --comments 5 --as-poster

# 不带数字:先列出所有非楼主评论(标注一级/二级),挑序号分析(支持 1,3-5 / all)
python tools/weibo/analyze.py --url "<帖子链接>" --comments
```

评论分析复用同一套 7 维判断(把「评论」当作正文,上下文带原帖摘要;二级回复还会带上它回复的一级评论原文),每条评论 3 次模型调用(判断/草拟/排序),约 2 秒/条。评论抓取走 `weibo.com/ajax/statuses/buildComments`(一级翻页 + 每条一级下的二级回复翻页,按 idstr 去重、跨页随机休眠 2–3s,上限 300 条;失败自动兜底移动端 hotflow)。楼主识别靠帖子作者 uid(statuses/show 返回),确定不了 uid 时会提示且不过滤。

## 成本与耗时

一次分析 = 1 次判断(7 题) + 1 次草拟 + 1 次排序(3 候选),实测约 1+2 秒、总费用约 $0.0001 量级(判断约 $0.00004,草拟与排序按所选模型计费)。费用与耗时打印在面板底部。

## 隐私

分析师触发时,帖子正文、作者昵称和你填的上下文会发送到你在 `config.json` 里配置的模型服务商(默认 OpenRouter);帖子抓取只调微博公开接口,且只在本地跑。本项目不运营任何中转服务器,请自行查看所选服务商与微博平台的条款。

## 设计说明

- `questions.py` — 微博场景题目集,结构对齐 `tools/jev/questions.py`(noul / choice / score,档位写具体情景)。state 形状是 `{"post": {"author", "text", "retweeted_text"?, "context"?}}`。
- `jev_client.py` — Jev 决策协议客户端,与 `tools/jev/jev_client.py` 同款重试/退避(429/529、超时、401/422 快速失败),差异在端点/密钥来自配置。
- `chat_judge.py` — 聊天模型判断兜底:同一套题目单轮发给 chat/completions,按题型把输出校正成 Jev 的 answers 形状(choice 置信度合成概率分布、score 档位钳制)。
- `reply_client.py` — 提示词移植自 Android 版 `ReplyClient.kt`(3 条、策略区分、40 字内),`parse_three` 容错覆盖单数组/多数组拼接/嵌套/按行等输出形态。
- `weibo_client.py` — 参考 weibo_scraper.py 的请求头配方,主用 `weibo.com/ajax/statuses/show?id=`,单条抓取、2 次退避重试,不做批量爬。

## MVP 范围外(可后续加)

批量逐帖分析(拉某用户最近 N 条)、微博私信对话、博主档案知识库(移植 `ContextBuilder`)、网页 UI、微博标注集与校准脚手架(可仿 `tools/jev/calibrate.py` + fixtures)。