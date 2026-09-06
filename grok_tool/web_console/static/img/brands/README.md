# Official brand icons

Mỗi tool reg dùng **đúng icon nhà phát hành**.

## Quy ước (bắt buộc cho tool mới)

1. File tên trùng `ToolMeta.id`:
   - `static/img/brands/{id}.svg`  (ưu tiên)
   - hoặc `{id}.png` / `{id}.webp`
2. Icon **app mark** chính thức: hình vuông bo góc, màu brand, mark trắng/màu gốc.
3. Không vẽ chữ cái giả (C, Z, …).
4. Không invert / nhuộm màu trên frontend.

Plugin không cần khai báo path. Server tự resolve:

`/static/img/brands/{id}.svg` → `.png` → `.webp`

Muốn file khác: `ToolMeta.brand_icon = "/static/img/brands/foo.svg"`

## Hiện có

| id | Nguồn |
|----|--------|
| grok | xAI Grok mark trên nền đen |
| heygen | Logo 4 cánh chính thức (png) |
| capcut | App icon teal CapCut |
| zai | Z mark từ `z-cdn.chatglm.cn/z-ai/static/logo.svg` |
| canva | App icon Canva (C vòng tròn #00C4CC) + `canva.png` Android |
| claude | Claude mark trên #D97757 |
| openai | OpenAI blossom trên #10A37F |
