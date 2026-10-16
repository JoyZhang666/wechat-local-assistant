"""Resolve only assets belonging to a selected message; never fetch or take keys."""
from datetime import datetime,timezone,timedelta
import hashlib
from pathlib import Path
import re
import stat
from .common import NamedMutex,inside


class MediaError(RuntimeError):
    def __init__(self,code,assets=None):
        super().__init__(code)
        self.code=code
        self.assets=assets or []


class MediaResolver:
    def __init__(self,provider,model,job_dir,progress=None):
        self.provider=provider
        self.model=model
        self.job_dir=Path(job_dir)
        self.progress=progress
        self.account_root=None
        self.media_paths=None
        self.processed=0
        self.asr=None
        self.image_keys=None
        self.image_keys_loaded=False

    def root(self):
        if self.account_root is None:
            self.account_root=self.provider.account_data_root()
        return self.account_root

    def resolve(self,message):
        out=self.job_dir/"snapshot"/"media-work"/message["id"]
        inside(out,self.job_dir/"snapshot")
        if not re.fullmatch(r"[a-f0-9]{64}",message["id"]):
            raise MediaError("invalid_message_id")
        if message["kind"]=="voice":
            from .voice import VoiceError,selected_voice_asset
            try:
                with NamedMutex(r"Local\WechatAiAssistantReadOnly"):
                    if self.media_paths is None:
                        self.media_paths=self.provider.voice_snapshot()
                    asset=selected_voice_asset(self.media_paths,message["chat_id"],message["local_id"],
                        message["server_id"],message["timestamp"],out)
                return [{"path":str(asset["silk_path"]),"name":"原语音.silk","kind":"voice_original","quality":"original"},
                        {"path":str(asset["wav_path"]),"name":"解码语音.wav","kind":"voice_audio","quality":"original"}]
            except VoiceError as error:
                assets=[]
                if error.asset.get("silk_path"):
                    assets=[{"path":str(error.asset["silk_path"]),"kind":"voice_original","name":"原语音.silk","quality":"original"}]
                raise MediaError(error.code,assets) from None
        if message["kind"]=="image":
            from .image import ImageError,selected_image_asset
            try:
                if not self.image_keys_loaded:
                    from .image_keys import ImageKeyStore
                    data_dir=getattr(self.model.settings,'data_dir',None)
                    if isinstance(data_dir,(str,Path)):
                        try:self.image_keys=ImageKeyStore(data_dir).load()
                        except Exception:raise MediaError('image_key_store_invalid') from None
                    self.image_keys_loaded=True
                return selected_image_asset(self.root(),message,out,image_keys=self.image_keys)
            except ImageError as error:
                raise MediaError(error.code,error.assets) from None
        return self.explicit_asset(message)

    def explicit_asset(self,message):
        """File/video cards need an exact local hint, never a whole-disk search."""
        root=self.root()
        month=datetime.fromtimestamp(message["timestamp"],timezone(timedelta(hours=8))).strftime("%Y-%m")
        chat_hash=hashlib.md5(message["chat_id"].encode()).hexdigest()
        base=root/"msg"/"attach"/chat_hash/month
        directories={"file":("File",),"video":("Video",),"emoji":("Emoji","Emoticon")}.get(message["kind"],())
        candidates=set()
        for hint in message.get("attachment_hints",[]):
            value=hint.get("value","")
            if not isinstance(value,str) or not value or "\0" in value:
                continue
            for directory in directories:
                scope=base/directory
                path=Path(value)
                if hint.get("type")=="path" and path.is_absolute():
                    try:
                        candidates.add(inside(path,scope))
                    except ValueError:
                        pass
                elif (hint.get("type")=="filename" and Path(value).name==value and value not in (".","..")
                        and not any(x in value for x in (":","\\","/"))):
                    candidates.add(scope/value)
                elif hint.get("type")=="hash" and re.fullmatch(r"[0-9a-fA-F]{32}",value):
                    for suffix in (".mp4",".webm") if message["kind"]=="video" else (".gif",".png",".webp"):
                        candidates.add(scope/(value.lower()+suffix))
        present=[]
        for path in candidates:
            if path.is_file():
                for part in (path,*path.parents):
                    if part.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        raise MediaError("attachment_link_rejected")
                present.append(path.resolve())
        if len(set(present))>1:
            raise MediaError("attachment_conflict")
        if not present:
            raise MediaError("attachment_not_found")
        return [{"path":str(present[0]),"name":present[0].name,"kind":message["kind"],"quality":"original"}]

    def annotate(self,message,assets):
        self.processed+=1
        if self.progress:
            self.progress({"phase":"recognizing","current":self.processed,
                           "message":"正在本机识别所选媒体，不上传内容。"})
        if message["kind"]=="image":
            available=[x for x in assets if x["kind"]=="image"]
            if not available:
                return {"status":"error","error_code":"decoded_image_missing"}
            result=self.model.analyze_image(available[0]["path"])
            result["input_quality"]=available[0].get("quality","unknown")
            return result
        available=[x for x in assets if x["kind"]=="voice_audio"]
        if not available:
            return {"status":"error","error_code":"decoded_voice_missing"}
        if not self.model.settings.asr_model_dir:
            return {"status":"error","error_code":"voice_transcription_not_configured"}
        self.model.stop()
        if self.asr is None:
            from .asr import LocalASR
            self.asr=LocalASR(self.model.settings.asr_model_dir,
                              work_dir=self.job_dir/"snapshot"/"asr-work")
        return self.asr.transcribe(available[0]["path"])

    def close(self):
        if self.asr is not None:
            self.asr.close()
            self.asr=None
