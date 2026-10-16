"""Synthetic offline image access; never inspect real account data or processes."""
import hashlib
from io import BytesIO
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from wechat_local.image import V2,ImageError,_verified_extension
from wechat_local.image_access import acquire_images
from wechat_local.image_keys import ImageKeyStore


class ImageAccessTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.xor=7
        suffix=hashlib.md5(b'7').hexdigest()[:4]
        self.root=self.base/('wxid_synthetic_'+suffix)
        self.root.mkdir()
        self.data=self.base/'private'
        self.key=hashlib.md5(b'7wxid_synthetic').hexdigest()[:16].encode('ascii')
        self.files=[]
        for index,color in enumerate(('red','blue')):
            buffer=BytesIO()
            Image.new('RGB',(256,256),color).save(buffer,format='JPEG')
            raw=buffer.getvalue()
            cipher=AES.new(self.key,AES.MODE_ECB).encrypt(pad(raw[:1024],16))
            tail=bytes(v^self.xor for v in raw[-16:])
            blob=V2+struct.pack('<II',1024,16)+b'\0'+cipher+raw[1024:-16]+tail
            path=self.base/(str(index)+'.dat')
            path.write_bytes(blob)
            self.files.append(path)

    def test_success_verifies_all_saves_dpapi_and_reuses(self):
        result=acquire_images(self.data,self.root,self.files,consent=True)
        self.assertEqual(result,{'images_verified':2,'reused':False})
        store=ImageKeyStore(self.data)
        self.assertEqual(store.load(),{'aes':self.key,'xor':7})
        old=store.path.read_bytes()
        self.assertNotIn(self.key,old)
        self.assertTrue(acquire_images(self.data,self.root,self.files,consent=True)['reused'])
        self.assertEqual(old,store.path.read_bytes())

    def test_consent_invalid_samples_cancel_timeout_and_old_key_preserved(self):
        with patch('wechat_local.image_access.plain') as read:
            with self.assertRaises(ValueError):acquire_images(self.data,self.root,self.files)
        read.assert_not_called()
        for samples in ([self.files[0]],[self.files[0]]*2):
            with self.assertRaises(ValueError):acquire_images(self.data,self.root,samples,consent=True)
        with self.assertRaises(KeyboardInterrupt):
            acquire_images(self.data,self.root,self.files,consent=True,stop=lambda:True)
        with patch('wechat_local.image_access.time.monotonic',side_effect=[0,100]):
            with self.assertRaisesRegex(ValueError,'超时'):acquire_images(self.data,self.root,self.files,consent=True)
        store=ImageKeyStore(self.data)
        store.save(bytes(16).hex(),7)
        old=store.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'原授权保持'):acquire_images(self.data,self.root,self.files,consent=True)
        self.assertEqual(old,store.path.read_bytes())
        with self.assertRaises(ValueError):store.save(self.key.hex(),7,replace=False)
        self.assertEqual(old,store.path.read_bytes())

    def test_corrupted_second_sample_cannot_authorize(self):
        blob=bytearray(self.files[1].read_bytes());blob[20]^=1
        self.files[1].write_bytes(blob)
        # A deliberately short synthetic deadline avoids enumerating the full space.
        with patch('wechat_local.image_access.time.monotonic',side_effect=[0,0,100]):
            with self.assertRaises(ValueError):acquire_images(self.data,self.root,self.files,consent=True)
        self.assertFalse(ImageKeyStore(self.data).path.exists())

    def test_image_limits_precede_pixel_loading(self):
        with patch('wechat_local.image.MAX_IMAGE_BYTES',4):
            with self.assertRaises(ImageError) as error:_verified_extension(b'12345')
        self.assertEqual(error.exception.code,'image_size_exceeded')
        from PIL import Image
        output=BytesIO();Image.new('RGB',(100,100)).save(output,format='PNG')
        with patch('wechat_local.image.MAX_IMAGE_PIXELS',100),patch('PIL.PngImagePlugin.PngImageFile.load') as load:
            with self.assertRaises(ImageError) as error:_verified_extension(output.getvalue())
        self.assertEqual(error.exception.code,'image_pixels_exceeded');load.assert_not_called()


if __name__=='__main__':unittest.main()
