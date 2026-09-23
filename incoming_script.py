# save this as test_cuda_torch.py
# Run with: python test_cuda_torch.py

import torch
import time

print("=" * 50)
print("PyTorch CUDA Test")
print("=" * 50)

# Check if CUDA is available
cuda_available = torch.cuda.is_available()

print(f"CUDA Available: {cuda_available}")

if cuda_available:
    print(f"GPU Name: {torch.cuda.get_device_name(0)}")
    print(f"CUDA Version: {torch.version.cuda}")
    print(f"PyTorch Version: {torch.__version__}")
    
    # Create tensors on GPU
    device = torch.device("cuda")
    
    print("\nRunning GPU computation test...")
    
    # Large matrix multiplication test
    size = 5000
    
    start = time.time()
    
    a = torch.randn(size, size, device=device)
    b = torch.randn(size, size, device=device)
    
    c = torch.matmul(a, b)
    
    # Wait for GPU to finish
    torch.cuda.synchronize()
    
    end = time.time()
    
    print(f"Matrix multiplication completed on GPU")
    print(f"Time taken: {end - start:.2f} seconds")
    
    print("\nGPU Memory Usage:")
    print(f"Allocated: {torch.cuda.memory_allocated(0) / 1024**2:.2f} MB")
    print(f"Reserved: {torch.cuda.memory_reserved(0) / 1024**2:.2f} MB")
    
    print("\nSUCCESS: NVIDIA GPU is working with PyTorch!")
    
else:
    print("\nCUDA is NOT available.")
    print("Possible reasons:")
    print("1. NVIDIA drivers not installed")
    print("2. CUDA toolkit missing")
    print("3. Installed CPU-only PyTorch")
    print("4. GPU not detected")