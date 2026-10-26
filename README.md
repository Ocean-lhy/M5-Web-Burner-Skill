# webburner skill v0.3

让 AI 助手（Codex / Cursor 等）帮你在 **M5Stack Web Burner**
(https://burner.m5stack.com) 上分享自己的固件：快速上传、更新版本和描述、查看下载/点赞统计。
也可以直接当命令行工具用。

## 要求

- Python 3.8+，只用标准库，不需要 `pip install`
- 一个 burner.m5stack.com 账号

## 安装

把整个 `webburner/` 文件夹复制到 skills 目录：

| 工具 | 位置 |
|---|---|
| Codex | `~/.codex/skills/webburner/` （Windows: `%USERPROFILE%\.codex\skills\webburner\`） |
| Cursor | `~/.cursor/skills/webburner/` （Windows: `%USERPROFILE%\.cursor\skills\webburner\`） |

```bash
# macOS / Linux
mkdir -p ~/.codex/skills ~/.cursor/skills
cp -r webburner ~/.codex/skills/ && cp -r webburner ~/.cursor/skills/
```

```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force "$HOME\.codex\skills","$HOME\.cursor\skills" | Out-Null
Copy-Item -Recurse webburner "$HOME\.codex\skills\"; Copy-Item -Recurse webburner "$HOME\.cursor\skills\"
```

## 第一次使用

1. 用 Edge 或 Chrome 打开 https://burner.m5stack.com 并登录。
2. 运行：

```bash
python ~/.codex/skills/webburner/scripts/webburner.py login
python ~/.codex/skills/webburner/scripts/webburner.py me
```

读不到浏览器时手动粘贴：F12 → Application → Local Storage →
`https://burner.m5stack.com` → 复制 `m5burner.accessToken` →
`webburner.py login --paste`。

token 只保存在本机 `~/.web_burner_bearer`（权限 0600），不要发给别人；
它和你的网站登录等价，过期后重新登录网站再 `login` 即可。

## 常用命令

```text
login / logout / me              登录、退出、当前账号
summary / stats / mine           总计、每个项目的下载/点赞/审核状态、我的项目
my-versions / edit-detail        我的版本、某版本的可编辑详情
categories / devices             可用的分类和设备
search / show / versions         公开查询
download                         下载某版本并校验 sha256
create                           上传全新项目
add-version                      给已有项目加新版本（自动防重复）
edit-version / edit-project      改版本信息 / 改项目名、描述、分类、设备
withdraw / relist / delete-version   下架 / 申请重新上架 / 删除版本
```

所有修改类命令都支持 `--dry-run`（只显示将要发送的内容），默认需要输入 `yes` 确认。
每个命令都有 `--help`。

## 跟 AI 说什么

- “用 webburner 登录并看看我的固件下载统计”
- “把 build/xxx_0x0.bin 作为 v1.2 上传到我的 XXX 项目，附上 GitHub 链接和这段更新说明”
- “在 Web Burner 上新建一个固件项目，设备选 StopWatch，分类 Tools”

## 说明

- 设为 PUBLIC 的新项目/新版本、公开项目的信息修改都需要官方审核，审核通过前页面显示旧内容。
- 非官方工具，基于网站前端使用的同一套接口；网站更新后接口可能变化。
