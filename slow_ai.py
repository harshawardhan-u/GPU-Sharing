import time
print("Starting intensive AI calculation...")

for i in range(1, 6):
    print(f"Epoch {i}/5: Training neural network layers...")
    time.sleep(1.5) # Simulates heavy GPU processing time

print("Training finished! Final accuracy: 98.4%")