# test_traversal.py
from agent.tools import read_file, write_file

print("=== 1. 测试高危写穿越 ===")
res_write = write_file("../main.py", "print('hacked')")
print(res_write)

print("\n=== 2. 测试合法沙箱写入 ===")
res_ok = write_file("test.txt", "hello sandbox")
print(res_ok)