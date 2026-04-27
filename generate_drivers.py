import csv
import random

LAT_MIN, LAT_MAX = 10.70, 10.85
LON_MIN, LON_MAX = 106.60, 106.75

first_names = ["An","Bình","Cường","Dũng","Huy","Khánh","Long","Minh","Nam","Phong",
               "Quân","Sơn","Tùng","Vinh","Đạt","Hiếu","Khoa","Phúc","Thắng","Tuấn",
               "Trung","Việt","Bảo","Hải","Lâm","Nghĩa","Phú","Quang","Thịnh","Trí"]

last_names = ["Nguyễn","Trần","Lê","Phạm","Hoàng","Huỳnh","Phan","Vũ","Võ","Đặng",
              "Bùi","Đỗ","Hồ","Ngô","Dương","Lý"]

middle_names = ["Văn","Thị","Hữu","Đức","Minh","Ngọc","Anh","Quang"]

drivers = []

for i in range(500):
    name = (
        random.choice(last_names) + " " +
        random.choice(middle_names) + " " +
        random.choice(first_names)
    )

    driver = {
        "id": i + 1,
        "name": name,
        "lat": round(random.uniform(LAT_MIN, LAT_MAX), 6),
        "lon": round(random.uniform(LON_MIN, LON_MAX), 6),
        "rating": round(random.uniform(3.5, 5.0), 1),
        "preference": random.choice(["Im lặng", "Trò chuyện"])
    }

    drivers.append(driver)

with open("drivers.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=drivers[0].keys())
    writer.writeheader()
    writer.writerows(drivers)

print("Đã tạo 500 drivers giả lập và lưu vào drivers.csv")