# VRChat Profile 匹配器

进入 VRChat 实例时自动扫描实例内所有玩家的资料（bio、链接等），按预设规则（关键词 / 正则 / 大模型 / 排除 / 共同好友）筛选，并在一个**深色小悬浮窗**里显示匹配到的人（头像、昵称、命中的规则）。

日志解析格式参考了 [VRCX](https://github.com/vrcx-team/VRCX)（`Dotnet/LogWatcher.cs`）。

## 界面

- **悬浮窗（游戏内用）**：无边框深色小窗，大小随匹配人数自动伸缩，只有一条标题栏 + 匹配列表：
  - 标题栏可拖动，`–` 折叠成一条细线，`⚙` 打开主面板；
  - 右键菜单：打开主面板 / 折叠 / 窗口置顶 / 透明度（100%~70%）/ 退出；
  - 每行 = 小头像 + 昵称 + 彩色规则标签（悬停看命中理由，单击看完整简介详情）；
  - 未保存过位置时始终贴屏幕右上角，拖动后的位置会记住。
- **主面板（配置用）**：**关闭主窗口即退出整个程序**（悬浮窗一并关闭）。所有更改**即时生效并自动保存**，无保存按钮。
  - **规则页**：可视化规则编辑器——列表 + 表单，支持新增/复制/删除/排序，关键词逐条添加/删除（回车快捷添加），正则可填测试文本实时验证，排除规则支持**搜索选人**（头像/名字/信任等级彩色列表）加入名单；
  - **状态页**：表格显示当前实例内的**所有人**（包括零命中的），每人标注检测状态（已检测/检测中/等待检测/获取失败）和命中的规则，命中的人整行高亮；
  - **设置页**：Cookie（可一键测试）、LLM 接口、扫描参数、悬浮窗外观（透明度/头像开关）、缓存清理、规则的**文件导入/导出**（导出可选择条目；导入可选条目并自动按内容去重）。

**高DPI 适配**：程序启动时自动声明 DPI 感知并按系统缩放系数放大全部界面尺寸（窗口、头像、行高、列宽），在高分屏上文字原生渲染不发糊、布局自动适配，无需手动调整。

## 工作原理

```
VRChat 日志文件 ──► LogTailer(线程) ──► Matcher(线程) ──► 悬浮窗 / 控制台
 Output Log/*.txt     进房/加入/离开事件      │
                                          ├─► VRChat API 拉资料（限流 ~2次/秒，简介缓存默认 4 小时过期）
                                          ├─► 本地规则：关键词 / 正则（bio、bioLinks、tags…）
                                          └─► LLM 规则：分块批量发给 OpenAI 兼容接口，结果按用户缓存
```

- **日志监控**：自动在两个位置寻找最新日志（兼容新旧版 VRChat）——新版 `%USERPROFILE%\AppData\LocalLow\VRChat\VRChat\output_log_*.txt`（日志直接在 VRChat 目录下）和旧版的 `Output Log\` 子目录；解析 `[Behaviour] OnPlayerJoined/OnPlayerLeft`、`Joining wrld_…` 等行；启动时会回读当前日志恢复出实例内已有人。
- **API 拉取**：用你提供的 `auth`（和可选 `twoFactorAuth`）Cookie 直接调 `api.vrchat.cloud/api/1`，默认限流 2 请求/秒，遇 429 自动按 `Retry-After` 退避并降速。VRChat 于 2025 年 9 月把 `/users/{id}` 改为精简响应（不再含简介），程序已改用 `/profile/{userId}` 端点获取简介/链接/头像。
- **玩家简介缓存**：按时间过期，默认 4 小时（设置页可调）。有效期内直接复用本地资料，过期后自动重新拉取；API 偶尔失败时会退回用旧缓存。
- **LLM 规则**：为省 token，**不**采用"每人一次请求"，也不把全房几十人塞进一次请求，而是**按 10 人一块分批并发**（可调）；缓存键 = 规则内容 + 用户 id + bio 内容哈希，**bio 不变就永远不会重复调用大模型**，新人进房只发没缓存的人。结果缓存按数量上限保存（默认 10000 条，超出淘汰最久未用的）。某批解析失败自动降级为逐人请求。

## 安装与运行

```bash
pip install -r requirements.txt   # requests + Pillow
python main.py                    # 悬浮窗 + 主面板
python main.py --demo             # 演示模式：假数据跑完整流程，不需要 Cookie/VRChat
python main.py --console          # 无界面，结果打印到控制台
python main.py --check            # 自检测试（35 项，不联网）
python main.py --clear-llm-cache  # 清空 LLM 结果缓存
```

游戏建议用**无边框窗口**模式运行（独占全屏时悬浮窗无法置顶显示）。

## 配置

所有配置都可以在主面板里改；也可以直接改 `config.json`（首次运行自动生成）。

**防丢保护**：每次保存都会自动在 `backups/` 里镜像一份最新配置（`config-latest.json`）并保留最近 7 天的每日快照。如果 `config.json` 被误删或损坏，下次启动会自动从最近的备份恢复，规则不会丢。（想彻底重置的话，把 `config.json`、`config.json.bak` 和 `backups/` 一起删掉即可。）

### 1. Cookie

浏览器登录 [vrchat.com](https://vrchat.com) 后，按 F12 → 应用(Application) → Cookie → `https://vrchat.com`：

- 复制 `auth` 的值 → 设置页的 auth 输入框（必填）
- 开了两步验证的话，再复制 `twoFactorAuth`（没开可留空）

设置页点「测试 Cookie」可验证是否有效。**Cookie 等同账号凭证，不要泄露给任何人。**

### 2. LLM（只有 AI 类规则需要）

任何 OpenAI Chat Completions 兼容接口都可以（OpenAI / DeepSeek / OpenRouter / 本地 Ollama 的 `http://localhost:11434/v1` 等）。

### 3. 规则（五种类型）

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| 关键词 | `keywords`、`mode`、`fields` | `mode`: 命中任一 / 全部命中；匹配不区分大小写；`fields`: 简介 / 链接 / 昵称 / 代词 / 状态描述 / 标签 |
| 正则 | `pattern`、`fields` | 常用 `-i` 忽略大小写已内置；表单里可填测试文本实时验证 |
| AI判断 | `prompt` | 自然语言描述判断标准，发给大模型逐人判断 |
| 排除 | `exclude_self`、`exclude_friends`、`users` | 命中的玩家从悬浮窗结果中**剔除**。`exclude_self`: 游戏客户端登录的自己；`exclude_friends`: Cookie 账号的好友（好友列表 15 分钟自动刷新）；`users`: 名单，按 uid 匹配（改名不影响），主面板里通过**搜索选人**对话框添加（显示头像/名字/信任等级），也可手动编辑 JSON |
| 共同好友 | `operator`、`count` | 玩家与 **Cookie 账号**的共同好友数量满足比较条件（默认 `≥5`）；只统计共同好友、不含共同群组，对方隐藏的共同好友也计入；数量按 (账号, 玩家) 缓存 24 小时 |

`config.json` 里的规则示例：

```jsonc
[
  {
    "id": "jp-learner",
    "name": "日语学习者",
    "type": "keyword",
    "keywords": ["日语", "日本語", "japanese"],
    "mode": "any",
    "fields": ["bio"]
  },
  {
    "id": "x-link",
    "name": "有X(推特)链接",
    "type": "regex",
    "pattern": "(?:twitter|x)\\.com/[A-Za-z0-9_]{1,20}",
    "fields": ["bio", "bioLinks"]
  },
  {
    "id": "creator",
    "name": "VRChat创作者",
    "type": "llm",
    "prompt": "判断该用户是否是 VRChat 相关的内容创作者（模型/地图制作者、主播、视频作者、摄影师等）。"
  },
  {
    "id": "exclude",
    "name": "排除",
    "type": "exclude",
    "exclude_self": true,
    "exclude_friends": false,
    "users": []
  },
  {
    "id": "mutual",
    "name": "共同好友",
    "type": "mutual_friends",
    "operator": ">=",
    "count": 5
  }
]
```

## 使用流程

1. `python main.py` 启动，右键悬浮窗 → 打开主面板 → 设置页填 Cookie（即时生效）、点「测试 Cookie」验证；
2. 进一个实例，悬浮窗标题栏会显示"房间 N 人"和扫描进度，符合规则的人陆续出现；
3. 悬停规则标签看命中理由，单击条目看完整简介和链接；
4. 游戏里觉得挡视线就 `–` 折叠成一条，或者右键调透明度。

## 注意事项

- 仅供个人学习与自用参考；使用 VRChat API 请遵守其[使用规范](https://hello.vrchat.com/)，默认限流已保守设置，请勿调高。
- LLM 判断有误报/漏报，关键结论请点开详情看简介原文。
- 匹配结果只在本机悬浮窗展示，不会向其他玩家发送任何信息。
