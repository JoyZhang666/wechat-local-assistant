# Windows安装与首次接入

版本：v1.13.0；2026-10-17（Asia/Shanghai）；技术用户源码正式版。

## 安装

安装含Tkinter的标准Python 3.12，在源码根目录：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-gui.txt
.venv\Scripts\python.exe scripts/create_gui_launcher.py --initialize-only --config "$env:LOCALAPPDATA/WechatLocalAssistant/settings.json"
.venv\Scripts\pythonw-local-only.exe scripts/wechat_ui.py gui
```

第三步不能省略：只创建本项目专用Python和必要运行库，不修改防火墙或自启；已有文件版本不同拒绝覆盖。已打开界面也可点击“一、模型 → 初始化本项目运行程序”。辅助接入另装`requirements-access.txt`；会员另装`requirements-membership.txt`；SILK另装`requirements-voice.txt`；可选Silero CPU VAD另装`requirements-asr.txt`。均使用本项目venv的pip；不自动下载模型。

## 六步新手引导

首次打开自动出现；也可点击主界面顶部“新手引导”。顺序为模型配置 → 本人微信接入 → 选择聊天 → 时间范围 → 预览／摘要或导出 → 阅读及完整包保存。

“上一步／下一步”只翻看说明；定位按钮打开对应设置页或将焦点移到主界面，不自动刷新聊天、测试模型、生成摘要或上传。“稍后再看”或关闭后记住步骤，点击入口继续；完成后入口从第一步重看。完成标记仅表示读过说明，真实配置、密钥验证和任务仍须用户操作。

进度文件在配置旁，内容仅为引导版本、步骤及看过／完成标记。写入失败会提示，不修改模型配置；数据目录、账号、密钥和聊天均不写入引导文件。

## 首次数据库接入

先在“选择模型与连接方式”保存私人数据目录及所选模型设置；“微信首次接入”选内置后端，保存后重新打开。

正常打开并登录Windows微信，保持运行，在助手点“连接／刷新”。程序自动识别微信程序、账号及记录位置，优先复用已保存授权；首次只需确认本机授权，不需要查找目录、程序或填写账号ID，也不要求先退出重开。成功自动加载聊天列表。具体范围见[首次接入与媒体组件](first-access.md)。

手动导入仅为默认收起的高级排错入口。安装和引导不取钥；首次必要的临时监听须本人当次授权，日常复用已有授权不重复取钥。监听可能受客户端版本及登录时机影响，失败明确提示并停止，不强关或自动重启微信。不要经命令行、聊天或Issue传递密钥。

已有部署可选外置后端，保留原读取组件目录；使用者自行核验其许可与兼容。公开版不附其DLL。

## 模型

- 本地：自行安装官方Ollama和兼容模型，填exe、模型目录、实际已安装标签与Ollama版本；独立回环端口默认11534，按需启动。升级后核验兼容。
- API：支持Chat Completions及JSON模式，填写HTTPS Base URL（如`https://api.example.com/v1`）、模型与Key。变更地址需重填Key，独立计费。
- 会员：安装会员依赖，在官方页面亲自登录、授予订阅额度权限；返回设置页选账户和返回模型，保存后点击“检查云端连接”。不导入其他工具的凭据。

云端使用本应用独立的按需请求解释器，保存设置时校验并创建，不修改共享程序。保存后可创建专用GUI快捷方式：

```powershell
.venv\Scripts\python.exe scripts/create_gui_launcher.py --config "$env:LOCALAPPDATA/WechatLocalAssistant/settings.json"
```

自定义配置路径需同步更改参数。此脚本不注册自启。严格本地隔离时，由用户为专用`python-local-only.exe`及`pythonw-local-only.exe`配置并验证Windows防火墙出站限制、保留回环；不要误限共享Python。源码不是网络沙箱，子进程还需各自验证。

## 媒体

图片密钥与数据库密钥独立。“图片解码”页提供单独授权的离线接入：本人选择账号目录及2—9张兼容V2加密JPEG样本，验证通过后DPAPI保存，不读取微信进程；手动入口保留。不匹配的旧授权不自动覆盖。仅缩略图、缺密钥或不支持的容器会标缺口。

图片理解需要视觉模型及匹配组件。语音需SILK解码、ASR程序和权重；Qwen3-ASR-1.7B Q8 GGUF、projector及llama.cpp b11516 Windows Vulkan部署与SHA-256配置见[媒体组件步骤](first-access.md#可选qwen语音组件)和[模板](../config/asr-runtime.example.json)。配置哈希存在时会在ASR启动前核验；未配置哈希的旧部署保持兼容，不视为已校验。模型可能误拒人声，保留原录音复核。

本版无一键ASR安装器，不附权重、程序或开发机runtime配置；未部署时保留消息与媒体、标识别未完成。模型及软件分别适用自己的许可证。

## 日常验收

先检查模型连接，再选择聊天／时间、预览、摘要或导出。云端连接检查只发送内置虚构材料，不读取微信。结束时间不包含，48／72小时快捷项滚动计算。打开完整HTML核查人物、日期、否定、引用和缺口，另存整个文件夹。取消／退出后检查本程序启动的模型与请求进程释放，不结束其他程序的模型进程。新机器仍需本人真实验收；开发机测试不替代它。
