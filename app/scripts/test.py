import app.pickscore.benchmark_service as b
for s in ["NVIDIA GeForce RTX 4050 Laptop GPU",
          "GeForce RTX 4050 Laptop GPU",
          "GeForce RTX 5060 Laptop GPU",
          "Intel Core i7-14650HX"]:
    print(repr(s), "->", repr(b._normalize(s)))