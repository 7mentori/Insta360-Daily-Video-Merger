# Changelog

All notable changes to this project are documented in this file.

## [V2.0.6]

### Added
- Added existing output detection during source analysis.
- Automatically checks whether an output file for the same recording date already exists.
- Dates with existing output files are unchecked by default to help prevent duplicate processing.
- Users can still manually select previously completed dates when needed.

### Improved
- Improved daily batch selection to account for existing output files.
- Preserved the existing output naming convention and non-overwrite behavior.

## [V2.0.5]

### Added
- Added detailed DEBUG logging for excluded non-4K video files.
- Added daily processing summaries, including included, excluded, and no-audio video counts.
- Added output verification using FFprobe.
- Added completion notification with system sound and a completion dialog.
- Added an Open Output Folder button.

### Improved
- Progress remains at a maximum of 99% until output verification and final file handling are completed.
- Elapsed time updates independently of FFmpeg progress reports.
- A day is marked as 100% complete only after successful verification and output placement.
- Improved Light Mode contrast for the Select None button.
- Improved batch selection warnings for dates containing mixed resolutions.

### Processing Rules
- Uses FFmpeg concat demuxer and Stream Copy.
- Only audio-bearing 3840 × 2160 videos are included.
- Videos without audio streams are skipped.
- Non-4K videos are excluded from daily merging.
- Mixed frame rates are supported without forced frame-rate conversion.
- Original source files remain unchanged.

## Version Notes

- V2.0.6 includes the existing-output detection feature introduced after V2.0.5.
- This changelog focuses on the documented functional changes between these versions.