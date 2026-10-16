# Windows安装与首次接入

版本：v1.13.0-rc.1；2026-10-16（Asia/Shanghai）；源码预发行。

## 安装

安装含Tkinter的标准Python 3.12，在源码根目录：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-gui.txt
.venv\Scripts\python.exe scripts/wechat_ui.py gui
```

会员模式另装`requirements-membership.txt`；SILK语音解码另装`requirements-voice.txt`；可选Silero CPU VAD另装`requirements-asr.txt`。模型与ASR权重不自动下载；不部署它们仍可启动界面、测试云端纯文字。

## 首次数据库接入

先在“选择模型与连接方式”保存私人数据目录及所选模型设置；“微信首次接入”选内置后端，保存后重新打开。

选择本人账号目录，其下应有`db_storage/contact/contact.db`、`session/session.db`及`message/message_*.db`。填本人账号ID（不是昵称、也不是带设备后缀的目录名）、已获授权的64位十六进制数据库密钥，勾选本次接入确认，点击验证导入。联系人和会话认证、结构验证通过后才DPAPI保存；错误不覆盖原授权。然后刷新聊天。

用户自行合法取得密钥；应用不提供自动提取，不自动附加／注入微信。提取可能影响客户端稳定性，需要另行说明和授权；开发机许可不适用于新用户。不要经命令行、聊天或Issue传递密钥。

已有部署可选外置后端，保留原读取组件目录；使用者自行核验其许可与兼容。公开版不附其DLL。

## 模型

- 本地：自行安装官方Ollama和兼容模型，填exe、模型目录、实际已安装标签与Ollama版本；独立回环端口默认11534，按需启动。升级后核验兼容。
- API：支持Chat Completions及JSON模式，填写HTTPS Base URL（如`https://api.example.com/v1`）、模型与Key。变更地址需重填Key，独立计费。
- 会员：安装会员依赖，在官方页面亲自登录、授予订阅额度权限；返回设置页选账户和返回模型，保存、合成测试。不导入其他工具的凭据。

云端使用本应用独立的按需请求解释器，保存设置时校验并创建，不修改共享程序。保存后可创建专用GUI快捷方式：

```powershell
.venv\Scripts\python.exe scripts/create_gui_launcher.py --config "$env:LOCALAPPDATA/WechatLocalAssistant/settings.json"
```

自定义配置路径需同步更改参数。此脚本不注册自启。严格本地隔离时，由用户为专用`python-local-only.exe`及`pythonw-local-only.exe`配置并验证Windows防火墙出站限制、保留回环；不要误限共享Python。源码不是网络沙箱，子进程还需各自验证。

## 媒体

图片密钥与数据库密钥独立，在“图片解码”页填写并DPAPI保存。格式／像素验证通过才显示；仅缩略图、缺密钥或不支持的WXGF等容器会标缺口。不会从预览窗口自动取钥。

图片理解需要支持视觉的本地模型及对应组件，只有文字标签不保证支持视觉。语音需SILK解码、ASR程序和权重。参考路线为Qwen3-ASR-1.7B Q8 GGUF、匹配audio projector及llama.cpp b11516 Windows Vulkan预览程序；接口见`src/wechat_local/asr.py`。`asr_model_dir/runtime.json`由用户单独核验，含执行程序、权重及哈希；可选`vad_model`启用Silero CPU门禁。模型可能误拒人声，须保留录音复核。

本版无一键ASR安装器，不附权重、程序或开发机runtime配置；未部署时保留消息与媒体、标识别未完成。模型及软件分别适用自己的许可证。

## 日常验收

先合成测试，再选择聊天／时间、预览、摘要或导出。结束时间不包含，48／72小时快捷项滚动计算。打开完整HTML核查人物、日期、否定、引用和缺口，另存整个文件夹。取消／退出后检查本程序启动的模型与请求进程释放，不结束其他程序的模型进程。新机器仍需本人真实验收；开发机测试不替代它。
