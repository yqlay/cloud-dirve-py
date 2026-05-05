# Cloud Drive

自建云盘系统。轻量、无数据库依赖，适合个人或小团队在内网/公网共享文件。

## 功能

### 文件管理
- 上传/下载/删除文件，支持批量操作
- 批量下载：选中多个文件打包为 zip 一次下载
- 新建文件夹、文件夹上传、按目录浏览
- 拖拽上传（拖到页面任意位置自动打开上传弹窗）
- 文件移动到任意文件夹
- 文件变化自动刷新（轮询检测，无需手动刷新）
- **多选方式**：Shift+点击 连选 / 拖拽 框选 / 逐个点击 checkbox
- 选中行高亮，顶部悬浮栏显示已选数量 + 取消选择按钮
- 表头 + 选择栏 sticky 悬浮，滚动不消失

### 远程终端
- 基于 WebSocket 的真 PTY 终端（pywinpty + xterm.js）
- 终端走主服务同端口，隧道/公网自动可用
- 多标签页：系统终端 + 控制台
- 会话持久化：切换页面终端不断开，刷新页面自动重连
- 输出历史回放：重连后恢复之前的终端内容
- 心跳保活：每 30 秒发一次心跳，防止连接被中间层断开
- 40 分钟闲置自动断开，2 小时无活动自动清理

### 用户系统
- 三级角色：`super_admin`（超级管理员） → `admin`（管理员） → `user`（普通用户）
- 五种权限：`download`、`upload`、`admin`、`console`、`terminal`
- 邀请码注册 + 管理员审批（邀请码支持多个，可留空表示开放注册）
- 普通用户可在线申请权限，super_admin 审批
- **权限管理页面**：可视化查看所有用户权限，一键授予/撤销（仅 super_admin）

### 在线管理
- 实时查看在线用户（IP、活跃时间、角色）
- 管理员可强制踢出用户下线
- 5 分钟无活动自动标记离线

### 控制台命令
控制台支持以下命令：

| 命令 | 说明 |
|------|------|
| `/online` | 查看在线用户 |
| `/kick <用户名>` | 强制下线 |
| `/approve <用户名>` | 批准注册 |
| `/reject <用户名>` | 拒绝注册 |
| `/grant <用户名> <权限>` | 授予权限（download/upload/admin/console/terminal） |
| `/revoke <用户名> <权限>` | 撤销权限 |
| `/read config` | 重载用户/权限/邀请码等配置 |
| `/read server` | 重载服务器配置（端口/域名等） |
| `/restart server` | 重启整个服务（需 30 秒内二次确认） |
| `/close` | 关闭所有终端会话 |
| `/quit` | 退出服务（需 30 秒内二次确认） |
| 直接输入文字 | 发送站内消息给所有在线用户 |

### 其他
- SPA 架构：所有页面共存于 DOM，切换不刷新，终端永不中断
- 站内消息：网页端右下角聊天面板 + 服务端控制台，双向实时通信
- 操作日志：记录所有操作（登录、上传、下载、删除等）
- 公网访问：支持自定义域名或 Cloudflare 快速隧道
- 深色 UI：自适应移动端

## 快速开始

```bash
# 克隆项目
git clone https://github.com/your-username/cloud-drive.git
cd cloud-drive

# 安装依赖
pip install -r requirements.txt

# 复制配置文件并编辑
cp config.example.json config.json
# 编辑 config.json，设置用户名、密码、邀请码

# 启动
python app.py
```

浏览器打开 `http://localhost:8080`

## 配置

编辑 `config.json`：

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8080,
    "debug": false,
    "secret_key": null,
    "public_url": null
  },
  "self_domain": {
    "enabled": false,
    "domain": ""
  },
  "expose_to_local_area_network": {
    "enabled": false
  },
  "terminal": {
    "enabled": true
  },
  "upload": {
    "max_file_size_mb": 5120,
    "max_folder_size_mb": 5120
  },
  "auth": {
    "users": [
      {"username": "admin", "password": "你的密码", "role": "super_admin", "permissions": []}
    ],
    "invite_code": ["邀请码1", "邀请码2"],
    "registration_enabled": true,
    "register_message_max_length": 128
  }
}
```

### 配置字段

| 字段 | 说明 |
|------|------|
| `server.host` | 监听地址，`0.0.0.0` = 所有网卡 |
| `server.port` | 服务端口 |
| `server.debug` | Flask 调试模式，生产环境必须 `false` |
| `server.secret_key` | Flask 会话密钥，`null` 则自动生成并持久化 |
| `server.public_url` | 公网访问地址，启动时自动写入，`null` 表示仅内网 |
| `self_domain.enabled` | 是否使用自定义域名 |
| `self_domain.domain` | 自定义域名，如 `drive.example.com` |
| `expose_to_local_area_network.enabled` | 是否启动 Cloudflare 快速隧道 |
| `terminal.enabled` | 是否启用终端服务 |
| `terminal.timeout_minutes` | 终端闲置断开时间（分钟），默认 40 |
| `upload.max_file_size_mb` | 单文件上传上限（MB） |
| `upload.max_folder_size_mb` | 文件夹上传总大小上限（MB） |
| `auth.users` | 用户列表 |
| `auth.users[].username` | 用户名，3-20 位字母数字下划线 |
| `auth.users[].password` | 密码，首次启动后哈希存储到 `auth.json` |
| `auth.users[].role` | 角色：`super_admin`/`admin`/`user`，不填默认 `user` |
| `auth.users[].permissions` | 额外权限列表，角色默认拥有的不需要配置 |
| `auth.invite_code` | 注册邀请码，为空/空数组/空字符串时表示开放注册 |
| `auth.registration_enabled` | 注册功能开关 |
| `auth.register_message_max_length` | 注册附言最大字节数，默认 128 |

### 角色与权限

| 角色 | 默认拥有 | 可被 super_admin 授予 |
|------|----------|----------------------|
| `super_admin` | 全部权限 | — |
| `admin` | upload, download, admin | console, terminal |
| `user` | download | upload, admin, console, terminal |

**权限说明：**

| 权限 | 能做什么 |
|------|----------|
| `download` | 浏览 + 下载文件 |
| `upload` | 上传 + 下载 + 删除 + 移动 + 新建文件夹 |
| `admin` | 管理用户注册、权限审批 |
| `console` | 访问网页端控制台 |
| `terminal` | 访问服务器终端 |

**各操作所需权限：**

| 操作 | 最低权限 | 说明 |
|------|----------|------|
| 浏览文件列表 | 登录即可 | 所有登录用户都能看到文件夹结构 |
| 下载文件 | `download` | user 默认拥有 |
| 上传文件 | `upload` | 包括单文件和文件夹上传 |
| 删除文件 | `upload` | 单个删除和批量删除 |
| 移动文件 | `upload` | 移动到其他文件夹 |
| 新建文件夹 | `upload` | — |
| 管理注册/权限 | `admin` | admin 默认拥有 |
| 控制台 | `console` | admin 默认拥有 |
| 服务器终端 | `terminal` | 需 super_admin 授予 |
| 修改密码 | 登录即可 | 在「设置」页面操作 |
| 查看日志 | 登录即可 | 所有用户都能看操作日志 |
| 发送消息 | 登录即可 | 站内群聊 |

**申请流程：**

1. 普通用户登录后，侧边栏底部会显示「申请上传」「申请终端」按钮
2. 点击后提交申请，进入待审批队列
3. super_admin 在「注册审批」页面的「待审批权限」区域批准或拒绝
4. 也可通过控制台 `/grant <用户名> <权限>` 直接授予

撤销权限：控制台 `/revoke <用户名> <权限>`，或 super_admin 在「权限管理」页面操作。

### 公网访问

三种模式，优先级从高到低，启动时自动写入 `server.public_url` 和 `server.innet_ip_url`：

| 模式 | `self_domain.enabled` | `expose_to_local_area_network.enabled` | `server.public_url` | `server.innet_ip_url` |
|------|----------------------|---------------------------------------|---------------------|----------------------|
| 自定义域名 | `true` | `false` | `https://{domain}` | `http://{内网IP}:{port}` |
| 快速隧道 | `false` | `true` | `https://xxx.trycloudflare.com` | `http://{内网IP}:{port}` |
| 仅内网 | `false` | `false` | `null` | `http://{内网IP}:{port}` |

`innet_ip_url` 始终生成，`public_url` 根据模式决定。

启动后始终显示：

```
内网访问: http://192.168.1.100:8080    ← 始终生成
公网访问: https://xxx                   ← 根据模式，或"未开启"
```

#### 模式一：自定义域名

适合已有域名和公网代理（Nginx、Cloudflare 隧道等）的用户。程序不管隧道，只负责生成 `public_url`。

**需要设置：**

```json
{
  "self_domain": {
    "enabled": true,
    "domain": "你的域名.com"
  }
}
```

```bash
python app.py
# 输出：
#   内网访问: http://192.168.1.100:8080
#   公网访问: https://你的域名.com（自定义域名，隧道由系统服务管理）
```

你需要自己确保 `https://你的域名.com` 能访问到本机的 `port` 端口。

#### 模式二：快速隧道

最简单，程序自动启动 cloudflared 快速隧道，适合临时分享，重启后地址会变。

**需要设置：**

```json
{
  "expose_to_local_area_network": {
    "enabled": true
  }
}
```

```bash
python app.py
# 输出：
#   内网访问: http://192.168.1.100:8080
#   正在启动快速隧道 ...
#   公网访问: https://xxx-xxx.trycloudflare.com
```

#### 模式三：仅局域网

两个都不开，`public_url` 为 `null`，只在局域网内使用。

**需要设置：**

```json
{
  "self_domain": { "enabled": false },
  "expose_to_local_area_network": { "enabled": false }
}
```

```bash
python app.py
# 输出：
#   内网访问: http://192.168.1.100:8080
#   公网访问: 未开启（仅内网访问）
```

### 邀请码

`invite_code` 字段支持以下格式：

| 值 | 行为 |
|----|------|
| `["码1", "码2"]` | 多个邀请码，任一即可注册 |
| `"单个码"` | 单个邀请码（兼容旧格式） |
| `""` 或 `[]` 或 `null` | 不需要邀请码，开放注册 |

### 热重载配置

运行中修改 `config.json` 后：

| 命令 | 重载范围 | 说明 |
|------|----------|------|
| `/read config` | 用户、权限、邀请码、注册开关、上传限制等 | 不中断服务 |
| `/read server` | 端口、域名等服务端配置 | 不中断服务 |
| `/restart server` | 全部配置 | 会重启服务进程（需二次确认） |

## 添加用户

**方式一：编辑 config.json**

在 `users` 数组中添加条目，重启后自动生效。

**方式二：邀请注册**

1. 将邀请码发给对方
2. 对方访问 `/register` 注册
3. 管理员在「注册审批」页面或控制台 `/approve` 批准
4. 用户登录后默认拥有 `download` 权限
5. 用户在侧边栏申请更多权限，super_admin 审批

## 项目结构

```
cloud-drive/
├── app.py                  # 主程序（路由、终端、控制台）
├── config.py               # 配置管理
├── auth.py                 # 认证、权限、注册审批
├── storage.py              # 文件存储引擎
├── logger.py               # 操作日志
├── messenger.py            # 站内消息
├── config.example.json     # 配置模板
├── config.json             # 运行时配置（已 gitignore）
├── auth.json               # 凭据哈希（已 gitignore）
├── requirements.txt        # Python 依赖
├── data/                   # 文件存储（已 gitignore）
│   └── .index.json         # 文件索引
├── logs/                   # 日志（已 gitignore）
└── templates/              # HTML 模板
    ├── base.html           # SPA Shell（侧边栏、终端、消息面板）
    ├── login.html          # 登录
    ├── register.html       # 注册
    ├── index.html          # 文件管理主页
    ├── settings.html       # 修改密码
    ├── logs.html           # 操作日志
    ├── admin_terminal.html # 远程终端页面
    ├── admin_online.html   # 在线用户管理
    ├── admin_registrations.html  # 注册审批
    ├── admin_permissions.html    # 权限管理
    └── terminal_page.html  # 终端 xterm.js 页面
```

## 安全机制

| 机制 | 说明 |
|------|------|
| 密码存储 | SHA-256 哈希，密码变更后持久化到 `auth.json`，不回写明文 |
| 登录限速 | 同一 IP 5 次失败后锁定 5 分钟 |
| 注册限速 | 同一 IP 3 次注册后锁定 10 分钟 |
| 邀请码 + 审批 | 无邀请码无法注册，注册后需管理员批准 |
| 权限控制 | 上传/下载/删除/移动/终端均需对应权限 |
| 路径遍历防护 | 上传路径自动过滤 `..` 序列 |
| 会话密钥 | 自动生成并持久化，重启不失效 |
| 终端隔离 | WebSocket PTY 会话独立，闲置自动断开 |

## 依赖

- Python 3.10+
- Flask
- flask-sock（WebSocket 支持）
- pywinpty（Windows PTY 终端支持）

```bash
pip install -r requirements.txt
```
