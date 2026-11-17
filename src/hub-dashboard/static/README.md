# Giao diện Skill Hub

Giao diện tiếng Việt dùng HTML, CSS, JavaScript và SVG của trình duyệt, không cần
thư viện bên ngoài hoặc CDN. Xem [hướng dẫn chạy](../../../deploy/hub-dashboard/README.md)
và [hợp đồng API](../README.md).

Các màn hình dùng cùng backend cục bộ:

| Màn hình | Chức năng |
| --- | --- |
| Tổng quan | Đếm tài nguyên và hiển thị trạng thái nguồn |
| Router Studio | Gửi yêu cầu định tuyến khi người dùng bấm chạy |
| Danh mục kỹ năng | Tìm từ khóa, phân trang, đọc nội dung và tạo bản sửa cách ly |
| Prompt và tri thức | Duyệt prompt và tài liệu nguồn tùy chọn |
| Sơ đồ năng lực | Xem node, quan hệ có bằng chứng và mở chi tiết |
| Agent | Hiển thị danh bạ tùy chọn, không suy ra trạng thái chạy |
| MCP và tích hợp | Hiển thị dữ liệu cấu hình và kiểm tra đích cố định |
| Telemetry | Đọc sự kiện đã lọc theo nguồn |
| Chất lượng | Hiển thị kiểm tra tĩnh và biên lai đánh giá tùy chọn |
| Cấu hình và cách ly | Quản lý cài đặt dashboard và tài nguyên được lưu cách ly |

Nguồn không có sẵn được hiển thị là chưa kết nối hoặc chưa ghi nhận. Tìm kiếm
ngữ nghĩa cần dịch vụ tương ứng; không mặc định giả định máy có GPU hay mô hình
cục bộ. Yêu cầu định tuyến có thể phát sinh phí và chỉ được gửi sau thao tác chủ
động, không gửi khi tải trang hoặc trong chu kỳ tự cập nhật.

API ghi yêu cầu CSRF token. Nội dung tài nguyên là dữ liệu không đáng tin; giữ
cơ chế lọc và hiển thị an toàn khi sửa giao diện. Dữ liệu import chỉ được lưu cách
ly, không tự kích hoạt kỹ năng hay thay đổi cấu hình agent.

Phím Escape đóng ngăn chi tiết hoặc hộp thoại. Ctrl+Enter (Cmd+Enter trên macOS)
trong ô Router Studio gửi yêu cầu. Có thể tắt tự cập nhật để giữ nguyên bảng khi
đang kiểm tra nội dung.

Kiểm tra cú pháp và logic nhập liệu từ thư mục gốc repository:

```sh
node --check src/hub-dashboard/static/app.js
node --check src/hub-dashboard/static/intake.js
node src/hub-dashboard/test_intake_ui.js
```
