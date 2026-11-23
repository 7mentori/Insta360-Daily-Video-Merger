請將 Windows 版 ffmpeg.exe、ffprobe.exe 放在此資料夾。
兩者應來自同一個 FFmpeg build。
若 FFmpeg 是 shared build，請一併放入所需 DLL。

V2.0.4 僅使用 FFprobe 分析與 FFmpeg concat demuxer / Stream Copy；不使用 NVDEC、CUDA filters、NVENC 或影片重新編碼。
