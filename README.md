# 🚀 GoRide -- Order Transportation System

Ứng dụng mô phỏng hệ thống đặt xe (tương tự Grab/GoJek) sử dụng Flask +
OSMnx.

## 📦 Tính năng

-   Đặt xe theo địa chỉ thực
-   Tính giá thông minh (fuzzy logic)
-   Lưu chuyến đi (trips.csv)
-   Đánh giá tài xế (reviews.csv)
-   Dashboard quản lý

## ⚙️ Cài đặt

``` bash
pip install -r requirements.txt
```

Tạo file `.env`:

    NGROK_AUTH_TOKEN=your_token_here

Chạy:

``` bash
python app.py
```

## 💾 Dữ liệu

-   trips.csv: lưu chuyến đi
-   reviews.csv: đánh giá
-   drivers.csv: tài xế
-   vouchers.csv: voucher

## 🔒 Bảo mật

Không commit `.env` lên GitHub.


## 👨‍💻 Project dùng cho học tập được thực hiện bởi nhóm sinh viên Đại học Kinh Tế TP.HCM - UEH bao gồm các thành viên:
- Đỗ Nguyên Anh
- Nguyễn Đức Duy
- Phan Bá Phú Sĩ
- Nguyễn Ngọc Bảo Uyên
