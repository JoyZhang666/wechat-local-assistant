# 微信本地助手

选择聊天和时间范围，把讨论整理为有原文依据的主题摘要，或保存成可离线阅读的聊天记录包。

**v1.13.0 · Windows技术用户源码正式版 · MIT**

本项目独立开发，不是微信或OpenAI官方产品。仅用于本人账号及有权处理的记录。

## 亮点

- **逐步新手引导**：首次打开自动提示，按模型、微信接入、聊天、时间、处理、结果六步说明；可暂停续看，也可点击顶部“新手引导”重看。导航不自动读取聊天或调用模型。
- **指定范围**：选择单聊／群聊及起止时间，先预览数量，再处理。
- **主题摘要**：按主题汇总决定、待办、未决问题和关注点；程序从原文提取引用，保存分段检查点，失败可恢复匹配分段。
- **完整记录包**：JSONL供Agent后续处理，Markdown及离线HTML供人阅读；整个文件夹含媒体与清单，便于长期保存和迁移。
- **微信多媒体**：呈现本机可恢复的图片；配置本地视觉及ASR组件后，可理解图片、转写语音并纳入摘要。原消息与模型注释分开。
- **三种文字模型**：本地Ollama、云端兼容API、ChatGPT／Codex会员授权；默认本地，云端每次确认发送范围。
- **按需运行**：不注册开机启动、不新增常驻监控；结束时释放本程序启动的模型／请求进程。
- **轻量源码**：不附模型、虚拟环境、原生解密DLL或真实聊天，依赖按功能安装。
- **自动连接微信**：正常打开并登录微信，点“连接／刷新”；自动识别程序、账号及记录位置，已有授权直接复用。首次必要时确认本机授权，普通流程不选目录、不填账号ID。

## 使用步骤

需要Windows 11、含Tkinter的Python 3.12，以及本人可用的微信4.x记录。详见[安装与接入](docs/public-install.md)。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-gui.txt
.venv\Scripts\python.exe scripts/create_gui_launcher.py --initialize-only --config "$env:LOCALAPPDATA/WechatLocalAssistant/settings.json"
.venv\Scripts\pythonw-local-only.exe scripts/wechat_ui.py gui
```

1. **选择模型与连接方式**：填写私人数据目录；纯云端文字摘要不必安装Ollama。
2. **正常登录微信并连接**：保持Windows微信已登录，在助手点“连接／刷新”。首次确认本人本机授权，程序自动识别；成功加载列表，日后直接复用。首次必要的组件为可选`requirements-access.txt`；不强关、不重启、不要求用户找文件夹。[操作和兼容范围](docs/first-access.md)。
3. **模型连接**：本地填写Ollama路径、模型目录及已安装标签；API填写HTTPS服务地址、模型和Key；会员在官方页面亲自授权后选返回的模型。
4. **检查连接**：点击“检查云端连接”，仅发送内置虚构材料，不读取微信。连接成功后刷新聊天、勾选对象、选择时间、预览数量。
5. **开始处理**：A页生成含媒体识别的摘要；B页选择只识别并导出，或识别后生成摘要。
6. **查看结果**：打开完整HTML；迁移时保存整个文件夹，包括`media/`和`manifest.json`。单个Markdown不能携带附件。

首次启动会出现新手引导。每步可打开相应设置或定位主界面，实际配置和处理后返回引导点“下一步”。“稍后再看”及关闭引导会保留位置，之后从顶部“新手引导”继续；完成后可随时从第一步重看。进度保存在配置文件旁的`*.onboarding.json`，只记录步骤和是否看过，不记录聊天或凭据；完成引导不代表配置验证通过。

密钥、API Key和会员令牌只在本机填写，由当前Windows用户DPAPI保护；不要放入Issue或Git提交。

## 模型与配置

| 模式 | 前提 | 资源与数据 |
|---|---|---|
| 本地Ollama | 自装Ollama与兼容模型 | 文字、图片处理留本机，内存和速度取决于模型 |
| 云端兼容API | Chat Completions兼容HTTPS接口、模型、Key | 独立计费，只发送本次确认的文字及媒体识别文本 |
| ChatGPT／Codex会员 | 符合条件的账号及官方授权 | 使用现有订阅额度，受账号、模型和服务限制 |

会员说明见[OpenAI官方文档](https://learn.chatgpt.com/docs/sign-in-with-chatgpt)。身份登录不等于已授予额度使用；本程序不读取Codex／OpenClaw凭据，不自动转API计费。

**本地模型参考机器**：ThinkPad X1 Carbon Gen 13，Core Ultra 7 258V、32GB内存、Intel Arc 140V核显；开发机使用Ollama 0.40.1和12B QAT量化模型做过有限合成测试。CPU及Intel GPU Vulkan路径有本机验证，独立安装仍须核验驱动、模型支持和实际GPU卸载。建议12B量化模型使用32GB内存并保留推理余量；云端纯文字模式不要求该配置。**本版不提供NPU支持，也不保证CPU／GPU同时满载。**

语音参考路线为Qwen3-ASR-1.7B Q8 GGUF、匹配audio projector及兼容llama.cpp Vulkan程序。模型、运行程序及驱动不随源码分发；具体接口见安装说明。

## 多媒体与完整性

- V2图片需要独立图片密钥；仅有缩略图时保留缩略图，不补造原图。限定20条记录／9图已人工验收显示及归属，不能推广为所有客户端或图片格式通过。
- 图片显示与图片理解是不同能力。缺密钥、媒体未下载、格式不支持或识别失败均在清单标注；文件导出成功不等于内容全部完整。
- 语音保留原件，配置SILK解码和ASR后可转写。视频、文件等保留能找到的原件，未实现的识别明确显示。本机没有的历史和附件不能恢复。
- 云端仅处理文字摘要；图片／语音在本地识别，衍生文字经本次确认后可以进入云端摘要。
- 摘要与转写可能漏项或误解，人物、否定及截止时间应人工复核；引用准确不代表结论全部正确。

## 隐私、状态与反馈

用户配置、缓存、日志、检查点、密钥及导出默认在`%LOCALAPPDATA%/WechatLocalAssistant`，不进入本仓库。解密缓存、摘要和导出是明文；DPAPI保护密钥不能替代磁盘权限和数据管理。

内置后端使用MIT算法，验证数据库页、只读取已提交WAL，失败不返回过期缓存。旧外置后端保留兼容路径，相关组件由使用者另行核验，公开包不附来源许可未核清的DLL。

这是面向技术用户的源码正式版，需要自行安装Python与所选模型组件。项目所有者已完成功能验收；独立隔离的全新Windows用户端到端验证及广泛客户端兼容仍未覆盖，不宣称安全认证或所有微信版本通用。构建和回退见[分发说明](docs/distribution.md)。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe scripts/wechat_ui.py --version
.venv\Scripts\python.exe scripts/build_public_source.py --check
```

Issue请提供版本、复现步骤和脱敏错误码；不要上传真实聊天、账号目录、完整日志、密钥或Token。

自有代码[MIT](LICENSE)；第三方代码保留[原许可与来源](third_party/wechat-decrypt/NOTICE.md)，见[许可范围](LICENSE-SCOPE.md)。模型及依赖各适用自己的许可证。
