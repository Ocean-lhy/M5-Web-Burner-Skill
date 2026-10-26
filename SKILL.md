---
name: webburner
description: 在 M5Stack Web Burner (burner.m5stack.com) 上分享自己的固件：登录、上传新固件项目、给已有项目加新版本、修改版本/项目描述、下架/重新上架、查看下载/点赞/审核统计。Use when the user wants to publish, update, or check stats of their own firmware on M5Stack Web Burner / M5Burner web.
---

# webburner — M5Stack Web Burner 固件分享助手 (v0.3)

脚本：`scripts/webburner.py`（仅用 Python 3.8+ 标准库）。下文 `wb` 代表
`python <skill目录>/scripts/webburner.py`。接口细节见 `API-MAP.md`。

## 规则（必须遵守）

1. **凭证**：只用网站登录后的 Bearer（localStorage `m5burner.accessToken`）。
   保存在 `~/.web_burner_bearer`（0600）或环境变量 `WEB_BURNER_BEARER_TOKEN`。
   **永远不要打印、回显、写进日志或聊天里**。脚本自己也不会打印 token。
2. **修改前先确认**：所有会改服务器的命令（create / add-version / edit-version /
   edit-project / withdraw / relist / delete-version）先跑 `--dry-run`，把输出的
   目标项目、版本、可见性、文件、描述给用户看，用户明确同意后再加 `--yes` 执行。
3. **不重复上传**：add-version 会先查 `versions scope=ALL`，同名版本存在就停止。
   失败后重试前，先跑 `wb versions <firmwareId>` 确认上次是否其实已成功。
4. **公开 = 需审核**：PUBLIC 的新项目/新版本、公开项目的描述修改都要等官方审核，
   审核前页面仍显示旧内容；PRIVATE 无需审核但别人看不到。
5. 描述直接写 Markdown，脚本会自动包成 `{"format":"markdown","content":...}`。
6. 只对用户自己的项目做修改（脚本会校验 firmwareId 属于当前账号）。

## 1. 登录

```bash
wb login                 # 自动从 Edge/Chrome/Chromium/Brave 默认目录读取（需先在浏览器登录网站）
wb login --browser chrome --profile "Profile 1"
wb login --paste         # 手动粘贴（不回显）
wb login --email you@example.com   # 邮箱+密码（密码交互输入）
wb me                    # 看当前账号和 token 过期时间
wb logout
```

手动获取 token：浏览器打开 https://burner.m5stack.com 并登录 → F12 →
Application(应用) → Local Storage → `https://burner.m5stack.com` →
复制 `m5burner.accessToken` 的值 → `wb login --paste`。
Token 过期（401）就重新登录网站后再 `wb login`。

## 2. 上传全新固件

```bash
wb categories            # 分类 id/名称
wb devices               # 设备 id/名称/代码
wb create --name "My Firmware" --version v1.0 \
  --file build/my_fw_0x0.bin --cover cover.png \
  --category Tools --device stopwatch \
  --description-file desc.md --version-description "首个版本" \
  --source-url https://github.com/you/repo --visibility PUBLIC --dry-run
# 用户确认后，把 --dry-run 换成 --yes
```

- `--category` / `--device` 可写 id、名称或代码，可重复或逗号分隔。
- 封面必填，建议 1280×720 PNG/JPG（网页会把非 GIF 封面转成 1280×720）。
- bin 用合并后的完整镜像（从 0x0 烧写）。
- 若账号下已有同名项目，脚本会拒绝并提示改用 add-version（服务器按名称匹配项目）。

## 3. 更新已有固件

```bash
wb mine                              # 找 firmwareId
wb resolve-mine "My Firmware"
wb versions <firmwareId>             # 已有版本（含审核中/私有）

# 新版本（不填的分类/设备/sourceUrl/可见性/封面沿用当前值）
wb add-version <firmwareId> --version v1.1 --file fw.bin \
  --version-description-file changelog.md --cover new.png \
  --source-url https://github.com/you/repo --dry-run

# 改某个版本的描述/版本名/sourceUrl/可见性/封面（未指定的字段保持不变）
wb edit-version <firmwareId> --version v1.1 --version-description-file changelog.md --dry-run

# 改项目名/项目描述/分类/设备（公开项目会生成待审核修订）
wb edit-project <firmwareId> --description-file desc.md --dry-run

wb withdraw <firmwareId> --version v1.0 --dry-run   # 下架
wb relist   <firmwareId> --version v1.0 --dry-run   # 申请重新上架（需审核）
wb delete-version <firmwareId> --version v1.0 --dry-run   # 不可恢复；最后一个版本需 --confirm-project-deletion
```

## 4. 统计

```bash
wb summary                 # 总固件数/下载/点赞/关注/积分
wb stats                   # 每个项目：下载、点赞、评论、状态、审核状态 + 各版本下载
wb stats --firmware-id <id> --json
wb my-versions             # 所有版本及审核状态 (PENDING 审核中 / PUBLISHED 已发布 / DELISTED 已下架 …)
wb edit-detail <id> --version v1.1   # 某版本的 sourceUrl、描述、项目修订审核状态
```

## 5. 公开查询（不需要登录）

`wb search <关键词>`、`wb show <id>`、`wb versions <id>`、
`wb download <id> --version v1.0 --output-dir out/`（输出 sha256）。

## 环境变量

| 变量 | 作用 |
|---|---|
| `WEB_BURNER_BEARER_TOKEN` | 直接提供 Bearer（优先于文件） |
| `WEB_BURNER_BEARER_FILE` | token 文件路径，默认 `~/.web_burner_bearer` |
| `WEB_BURNER_BASE_URL` | 默认 `https://burner.m5stack.com` |
| `WEB_BURNER_NO_BROWSER=1` | 禁止在 token 缺失/过期时自动读取浏览器 |
| `WEB_BURNER_PASSWORD` | 仅 `login --email` 使用（可选） |

## 汇报给用户

上传/修改后报告：项目名、firmwareId、versionId、版本名、可见性、状态（多半是
PENDING 待审核）、项目页 `https://burner.m5stack.com/firmware/<firmwareId>`。
