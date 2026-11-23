# Insta360 Daily Video Merger

**Version: V2.0.6**

一款專為 Insta360 使用者設計的 Windows 桌面影片整理工具，可依照影片拍攝日期，自動分析、分類並合併每日的 4K MP4 影片。

程式使用 Python、CustomTkinter 與 FFmpeg 開發。合併時採用 FFmpeg concat demuxer 搭配 Stream Copy，不重新編碼影片，避免不必要的畫質損失與長時間等待。

## ✨ Features

- **依日期自動分類：** 分析影片檔名，依拍攝日期分組。
- **每日影片合併：** 將同一天符合條件的 4K MP4 影片合併成單一檔案。
- **無重新編碼：** 使用 Stream Copy，保留原始影片串流，避免重新壓縮。
- **自動排除無音訊影片：** 沒有音訊串流的影片不會納入合併。
- **解析度檢查：** 僅納入解析度為 3840 × 2160 的影片。
- **混合解析度提醒：** 同一天包含不同解析度影片時，顯示提醒並提供選擇。
- **避免重複輸出：** 若輸出資料夾已存在相同日期的成品，該日期預設不勾選。
- **原始檔案保護：** 不會修改、刪除、移動或重新命名來源影片。
- **詳細執行紀錄：** 記錄影片分析、排除原因、合併進度與驗證結果。
- **深色／淺色介面：** 支援 Dark Mode 與 Light Mode。

## 🖥️ System Requirements

- Windows 10 / Windows 11
- Python 3.10 或更新版本（建議 64-bit）
- FFmpeg 與 FFprobe
- CustomTkinter 及其他 Python 套件（依 `requirements.txt` 安裝）

本程式不使用 NVIDIA GPU、NVDEC、CUDA 或 NVENC，也不需要 NVIDIA 顯示卡或 CUDA 環境。

## 📁 Project Structure

```text
Insta360-Daily-Video-Merger/
│
├── insta360_daily_video_merger.py
├── requirements.txt
├── README.md
├── CHANGELOG.md
├── LICENSE
├── .gitignore
│
└── ffmpeg/
    └── README_place_FFmpeg_here.txt
```

**注意：** GitHub 專案不包含 FFmpeg 執行檔及其 DLL。使用者必須自行準備 Windows 版本的 FFmpeg，並放置於程式指定的 `ffmpeg` 資料夾。

## 🚀 Installation

### 1. Download the project

下載 GitHub Repository 的 ZIP，或使用 Git Clone：

```bash
git clone https://github.com/7mentori/Insta360-Daily-Video-Merger.git
```

### 2. Install Python dependencies

開啟命令提示字元或 PowerShell，切換至專案資料夾：

```bash
pip install -r requirements.txt
```

### 3. Prepare FFmpeg

下載適用於 Windows 的 FFmpeg build，將以下檔案及執行時需要的 DLL 放入專案的 `ffmpeg` 資料夾：

- `ffmpeg.exe`
- `ffprobe.exe`
- FFmpeg build 所需的 DLL

請確認 FFmpeg 與 FFprobe 可以正常執行。

## ▶️ Usage

在專案資料夾中執行：

```bash
python insta360_daily_video_merger.py
```

操作流程：

1. 選擇來源影片資料夾。
2. 選擇合併後影片的輸出資料夾。
3. 等待程式分析影片並依日期分組。
4. 確認各日期的影片數量與狀態。
5. 勾選需要合併的日期。
6. 開始合併，等待程式完成輸出驗證。

## 🎬 Video Processing Rules

### Supported filename formats

程式依照影片檔名中的日期與時間資訊進行分析，支援以下格式：

```text
VID_YYYYMMDD_HHMMSS_NNN.mp4
VID_YYYYMMDD_HHMMSS_00_NNN.mp4
```

第二種格式為舊版檔名格式。

程式僅掃描來源資料夾第一層，不會遞迴搜尋子資料夾。

### Video eligibility

| Video condition | Processing |
|---|---|
| 有音訊，解析度為 3840 × 2160 | 納入合併 |
| 有音訊，但不是 3840 × 2160 | 排除 |
| 沒有音訊串流 | 排除 |

無音訊影片不會計入合併影片數量、總時長與總容量。

### Resolution handling

- 只有符合 3840 × 2160 的影片會納入每日合併。
- 同一天若存在其他解析度的影片，該日期預設不勾選，並以紅色文字提醒。
- 使用者仍可手動勾選混合解析度日期，但程式會在合併前顯示警告。
- 若影片只有非 4K 或無音訊影片，沒有符合條件的 4K 影片，該日期無法選擇合併。
- 若同一天同時存在非 4K 與無音訊影片，介面會顯示相應的狀態提示。

建議將不同解析度的影片先使用 Insta360 Studio 轉換，再進行合併。

### Frame rate handling

程式不會強制統一 FPS。

例如，同一天的 4K 影片可以包含 30 FPS、60 FPS 或其他幀率，合併時仍採用 Stream Copy。不同幀率的影片合併後，輸出可能呈現 VFR（Variable Frame Rate）。

## 📦 Output

每日輸出檔名：

```text
YYYY-MM-DD.mp4
```

若相同檔名已存在，程式會使用流水號建立新檔案，例如：

```text
2026-11-23.mp4
2026-11-23_2.mp4
2026-11-23_3.mp4
```

不會覆蓋既有檔案。

### Existing output detection

分析來源影片時，程式會檢查輸出資料夾是否已有相同日期的合併成品。

若已有相同日期的輸出檔案，該日期預設不勾選，以避免重複處理。使用者仍可依需求手動重新勾選。

### Original file protection

來源影片僅供讀取與分析。

程式不會修改、刪除、移動或重新命名任何來源影片。

## 🔍 Output Verification

每日影片合併完成後，程式會使用 FFprobe 驗證輸出檔案：

- 檔案存在且大小大於 0。
- 容器及影片串流可正常讀取。
- 存在影片與音訊串流。
- 影片解析度為 3840 × 2160。
- 輸出影片時長與納入合併的來源影片總時長差異不超過 `max(2 seconds, 1%)`。

程式也會記錄 `r_frame_rate` 與 `avg_frame_rate`，不會強制要求輸出為固定 59.94 FPS。

若合併過程出現 Non-monotonic DTS / Non-monotonous DTS 警告，該日期的合併會判定失敗，不會改用整日重新編碼的方式處理。

## 📝 Logging

程式會產生詳細執行紀錄，包含：

- 來源影片分析結果。
- 被排除影片的檔名及排除原因。
- 非 4K 影片的解析度、編碼資訊、像素格式、位元深度、幀率與音訊資訊。
- 每日納入與排除的影片數量。
- 合併進度、執行時間及輸出驗證結果。
- 使用者手動選擇混合解析度日期時的警告。

Log 檔案會記錄於程式所在資料夾。

## ⚙️ Application Settings

使用者設定檔儲存於：

```text
%APPDATA%\Insta360DailyVideoMerger\settings.json
```

程式會記住來源資料夾、輸出資料夾及外觀模式。

首次啟動預設使用 Dark Mode。

## 🛑 Error Handling

- 單一日期合併失敗時，會清除該日期未完成的暫存檔與不完整輸出，並繼續處理其他日期。
- 使用者按下停止時，程式會中止目前 FFmpeg 工作、停止後續批次，並清理未完成的暫存資料。
- 已完成的日期不會因為後續日期失敗或使用者停止而被刪除。
- 只有完成輸出驗證及檔案移動後，該日期才會顯示 100% 完成。

## 📜 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## ⚠️ Disclaimer

This project is an independent community-developed utility and is not affiliated with, endorsed by, or officially associated with Insta360.

Insta360 and related product names are trademarks of their respective owners.