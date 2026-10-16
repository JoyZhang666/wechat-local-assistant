# 构建与分发

版本：v1.13.0-rc.2；源码预发行候选；2026-10-16（Asia/Shanghai）。

`src/wechat_local/__init__.py`是软件版本的唯一代码来源，CLI／状态／构建引用它。历史版本不重写，依赖版本独立管理。

```powershell
python scripts/build_public_source.py --check
python scripts/build_public_source.py --output outputs/public/wechat-local-assistant-v1.13.0-rc.2.zip
```

固定白名单仅含程序、公开文档、合成测试、最小CI和许可证；公开README映射为根`README.md`。不包含开发历史、运行目录、原供应包、模型、虚拟环境、DLL、真实资料和旧交付。输出不覆盖旧包，不上传、不安装、不注册后台任务。

包内生成`SHA256SUMS.txt`和`RELEASE-STATUS.txt`，写完回读全部成员及哈希。哈希只证明字节一致，不等于审计；审计记录放在发布副本之外。

基础4个直接依赖，会员另装2个、SILK语音另装1个、可选CPU VAD另装3个；权重与ASR程序单独部署。文件减少不代表运行提速，须使用固定合成输入测量。

内置读取器不依赖原解密DLL；MIT上游固定提交与许可见`third_party/wechat-decrypt`。验证联系人、会话后DPAPI保存，不携带开发机授权，不自动提取密钥。旧外置配置保持，不自动迁移。

## 验证边界

合成测试涵盖文字后端、筛选、引用、检查点、图片、语音、包迁移及异常。真实验收限定20条／9图，仅确认显示及归属，不扩大为媒体理解质量证明，不自动重发真实聊天。

独立隔离的全新Windows用户端到端验收和广泛客户端兼容仍未完成；临时目录与venv不等于系统隔离，CI只证明其实际运行的测试。已知缺口保留，预发行不作全面安全认证。

## 回退

恢复此前冻结源码与原私人配置即可回退；不自动删除原模型、聊天或历史交付。新内置授权／缓存独立存放。恢复原`provider_dir`并选`provider_backend=external`可继续旧后端；不要混用其他账号缓存。

公开源码与私人导出包是不同交付，后者绝不进入GitHub或公共Release。更新后先合成测试，再由使用者本人验收真实范围。
