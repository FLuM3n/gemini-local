# Gemini Local

一个轻量 Gemini 网页版终端助手，同时提供 Zotero 可调用的本地 API。Windows 与 macOS 共用同一套程序代码，默认一个窗口同时运行两种用途。

## 下载

在 [Releases](https://github.com/FLuM3n/gemini-local/releases/latest) 下载对应系统的 ZIP，并完整解压。程序不包含任何人的 Google 账号、Cookie 或 API Key。

- **Windows x64**：下载 Windows 包，双击 `Gemini.bat`，随包已有 Python，无需另装。
- **macOS（Apple Silicon / Intel）**：下载 macOS 包，先安装 [Python 3.12](https://www.python.org/downloads/macos/)，再双击 `Gemini.command`。首次会在程序目录创建 `.venv` 并安装依赖，以后复用。也支持 Python 3.11–3.13。

macOS 如未直接执行 `.command`，可以打开终端，进入解压目录后运行 `bash Gemini.command`。不需要关闭系统安全功能。

## 首次登录

1. 使用 Chrome 或 Edge 打开扩展管理页，开启开发者模式，选择“加载已解压的扩展”，选中程序内的 `browser-extension` 文件夹。
2. 启动 Gemini。程序会打开本机登录连接页，并提示登录 Gemini 官方网页。
3. 在同一浏览器登录自己的 Google 账号，允许扩展将会话交给本机程序。出现登录验证成功后即可聊天。macOS 会优先打开已安装的 Chrome/Edge；Safari 不能使用这套扩展。
4. 各电脑分别登录。Windows 会话用当前用户 DPAPI 加密保存；macOS 会话保存到当前用户的系统钥匙串。首次访问钥匙串时，按系统提示处理。

程序默认只监听 `127.0.0.1`，不对局域网开放。扩展只在本次连接有效时向本机传递登录会话，不会把 Cookie 发送到本项目或作者的服务器。Google 网页协议、风控或设备绑定变化仍可能导致会话失效，此时重新启动并按提示登录。

## 在 Zotero 中使用

保持 Gemini 窗口运行，在 AIdea 的自定义 API 中选择“Gemini 网页版（本地）”：

- 地址：`http://127.0.0.1:8100/v1/`
- Key：默认留空；如自行配置了 `GEMINI_LOCAL_API_KEY`，填写相同值。
- 模型：点击获取列表，按网页账号实际返回的模型选择，不依赖预设名称。

支持全文文字、对话历史、扫描图片、普通 JSON 和流式回答。内置引用标记转换，避免 Gemini 网页端把 Zotero 的 `[[cite:编号]]` 改成空 `[]`。已经丢失编号的旧回答需要重试，程序不会猜测来源。

终端和 Zotero 的对话彼此独立，同时发起的请求会排队。共用同一 Gemini 网页账号的用量限制。终端默认 Extended，Zotero 默认 Standard，互不随对方的思考设置变化。切换终端模型不会自动替 Zotero 切换模型。

输入 `/exit` 或关闭窗口，两者一起停止。Ctrl+C 用于中断当前输入或回答接收，不保证模型端立即停止计算或返还用量。

## 终端命令与选项

`/models` 查看真实模型列表；`/model 序号或名称` 切换；`/clear` 新开终端对话；`/thinking standard` 或 `/thinking extended`；`/quota` 查询账号返回的额度；`/exit` 退出。

启动参数可传给对应系统的入口，例如：

```text
--port 8101                 使用另一个本机端口，Zotero 地址也需相应修改
--thinking standard         终端使用 Standard
--api-thinking extended     Zotero 默认使用 Extended
--terminal-only             只开终端
--serve --thinking standard  只开本地 API
--check                     仅检查账号与模型，不发送聊天
```

## 本机数据与隐私

- Windows：`%LOCALAPPDATA%/GeminiTerminal`
- macOS：`~/Library/Application Support/GeminiTerminal`

可选设置位于上述目录中的 `.env.gemini`，字段示例见 `.env.example`。登录记录、SDK Cookie 缓存和配置均不应分享或上传 GitHub。macOS 的 SDK 缓存仍属于敏感数据，程序会限制应用数据目录权限。

本仓库只保存程序和第三方 SDK 源码。Windows 原生运行时仅随对应 Release 提供；macOS 使用自己的 Python 和原生依赖，不运行 Windows 二进制。`vendor/gemini_webapi/` 保留原随包 SDK 快照，以保持模型和登录接口一致，许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 开发验证

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用本机 HTTP 和虚构材料，不调用真实 Gemini，不消耗网页问答额度。GitHub Actions 检查 Windows、Apple Silicon macOS 和 Intel macOS；macOS 使用临时测试钥匙串验证原生存取。测试不代表已经在你的 Mac 上完成 Google 网页登录，实际登录需本人操作。

项目使用 AGPL-3.0 许可，非 Google 官方产品。
