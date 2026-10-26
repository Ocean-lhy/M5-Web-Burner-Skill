# Web Burner API Map (v0.3)

Base: `https://burner.m5stack.com/api/v1` (override host with `WEB_BURNER_BASE_URL`).
Mapped from the public web frontend. Responses are `{code, msg, data}`; paged lists
return top-level `rows` + `total` (no `data`).

## Auth

| Mode | Credential | Header |
|---|---|---|
| Public | none | — |
| Bearer JWT | localStorage `m5burner.accessToken` after login on the website | `Authorization: Bearer <JWT>` |

The frontend also sends `X-Request-Id: <uuid>` and `language: CN|US|JP` (optional).
`POST /auth/login` body: `{"email","password","rememberMe","deviceName"}` → `data.accessToken`.
The Bearer expires (JWT `exp`); on 401 log in again.

## Public (no auth)

| Method | Path | Notes |
|---|---|---|
| GET | `/firmwares` | search: `keyword`, `sort` (`LATEST`…), `pageNum`, `pageSize` |
| GET | `/firmwares/{fid}` | detail: `categories`, `supportedDevices`, `currentVersion`, `sourceUrl`, `statistics{downloadCount,likeCount,commentCount}`, `developer` |
| GET | `/firmwares/{fid}/versions` | `scope=PUBLIC` (default) or `ALL` (owner + Bearer) |
| GET | `/firmwares/{fid}/versions/{vid}/download` | BIN download |
| GET | `/firmware-categories` | `categoryId`, `categoryCode`, `categoryName` |
| GET | `/devices` | `deviceId`, `deviceCode`, `deviceSlug`, `deviceName` |
| GET | `/firmwares/rankings/weekly`, `/firmwares/rankings/all-time` | `limit` |
| GET | `/developers/{id}`, `/developers/{id}/firmwares` | public developer pages |

## Account (Bearer, read)

| Method | Path | Notes |
|---|---|---|
| GET | `/users/me` | `userId`, `username`, `nickname`, `email`, `roleKey`, `status` |
| GET | `/users/me/developer-summary` | `totalFirmware`, `totalDownloads`, `totalLikes`, `totalFollowers`, `totalPoints` |
| GET | `/users/me/firmware-projects` | my projects; `pageNum`, `pageSize`, `visibility=ALL` |
| GET | `/users/me/firmwares` | my versions; same params |
| GET | `/firmwares/{fid}/versions/{vid}` | owner edit-detail: `sourceUrl`, descriptions, `projectRevisionId/Status`, `reviewReason` |
| GET | `/users/me/followers`, `/users/me/received-likes`, `/users/me/points/details` | engagement details |

Row fields of `firmware-projects` / `firmwares`: `firmwareId`, `firmwareName`, `versionId`,
`versionName`, `visibility`, `status`, `reviewReason`, `likeCount`, `coverUrl`, `binFileName`,
`categories`, `supportedDevices`, `projectRevisionId`, `projectRevisionStatus`, `uploadedAt`.
Version list items add `current`, `downloadCount`.

## Mutations (Bearer) — same calls the website makes

| Method | Path | Body |
|---|---|---|
| POST | `/firmwares` | multipart. **Create** a project, or **add a version** when `firmwareName` exactly equals one of your projects (no firmwareId is sent). Fields: `firmwareName`, `versionName`, `sourceUrl`, `categoryIds` (JSON array), `deviceIds` (JSON array), `firmwareDescription` (new project), `versionDescription`, `visibility` (`PUBLIC`/`PRIVATE`), `binFile`, `coverFile` (required by the UI for new projects; omit to keep the current cover on a new version). → 201 |
| PUT | `/firmwares/{fid}/versions/{vid}` | multipart: `versionName`, `versionDescription`, `sourceUrl`, `visibility`, optional `coverFile` |
| PATCH | `/firmwares/{fid}` | multipart: `firmwareName`, `firmwareDescription`, `categoryIds`, `deviceIds`. On a public project this creates a revision (`projectRevisionStatus=PENDING`) for review |
| POST | `/firmwares/{fid}/versions/{vid}/withdraw` | withdraw (hide) a version |
| POST | `/firmwares/{fid}/versions/{vid}/relist-request` | ask to relist; reviewed |
| DELETE | `/firmwares/{fid}/versions/{vid}?confirmFirmwareDeletion=false|true` | delete; deleting the last version needs `true` (else `FIRMWARE_DELETE_CONFIRM_REQUIRED`) and removes the project |

Descriptions are JSON strings: `{"format":"markdown","content":"..."}`.
Covers: `image/*`; the website re-renders non-GIF covers to 1280×720 PNG.
Status values seen: `PENDING` (in review), `PUBLISHED`, `DELISTED` (withdrawn); `reviewReason` explains a rejection.
Project page URL: `https://burner.m5stack.com/firmware/{firmwareId}`.

## Other Bearer endpoints (not used by the script)

Likes/follows (`PUT/DELETE /firmwares/{id}/like|follow`, `/users/{id}/follow`),
shares (`POST /firmware-shares`, `GET /users/me/firmware-shares`), notifications
(`/notifications`), feedback (`POST /feedbacks`), profile (`PATCH /users/me`).
