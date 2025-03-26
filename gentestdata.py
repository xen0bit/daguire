import random
import os
with open('random.txt', 'a') as f:
    for i in range(0, 1_000_000):
        f.write(os.urandom(random.randrange(1, 8)).hex()+"\n")
