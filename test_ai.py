import torch
print("Hello from the Docker Container!")
print("Is CUDA GPU available?", torch.cuda.is_available())