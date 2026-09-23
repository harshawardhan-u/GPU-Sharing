import sys

try:
    import torch
    print("✅ PyTorch is installed!")
    
    if torch.cuda.is_available():
        print(f"🔥 GPU DETECTED: {torch.cuda.get_device_name(0)}")
        print(f"Total VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.2f} GB")
    else:
        print("⚠️ Running on CPU (No GPU detected inside container).")
        
except ImportError:
    print("❌ PyTorch is not installed in this environment.")
    
print(f"💻 Python Version: {sys.version}")