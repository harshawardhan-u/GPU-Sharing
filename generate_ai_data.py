import os
# Read local file
with open("data.txt", "r") as f:
    print("Read inside Docker:", f.read())

# Write a NEW file to prove data travels back
with open("ai_model_weights.txt", "w") as f:
    f.write("SUPER SMART AI BRAIN CREATED IN DOCKER")