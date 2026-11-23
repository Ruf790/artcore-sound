"""ArtCore Sound — an audio editor for Windows: waveform per channel, editing with unlimited undo kept on disk,
processing, effects and analysis; files opened and saved through FFmpeg, playback through PortAudio (WASAPI)."""
VERSION_TUPLE = (1, 0, 0, 0)
VERSION       = "1.0.0"
APP_TITLE     = "ArtCore Sound"
APP_ID        = "ArtCore.Sound"
REPO          = "Ruf790/artcore-sound"

AUDIO_EXT = (".wav", ".flac", ".mp3", ".m4a", ".aac", ".ac3", ".eac3", ".ec3", ".dts", ".dtshd", ".thd", ".mka",
             ".ogg", ".oga", ".opus", ".wma", ".ape", ".wv", ".aiff", ".aif", ".aifc", ".alac", ".mp2", ".tta",
             ".caf", ".w64", ".au", ".snd", ".amr", ".mpc", ".spx")
VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".asf", ".flv", ".webm", ".ts", ".m2ts", ".mts",
             ".mpg", ".mpeg", ".vob", ".mxf", ".3gp", ".ogv", ".rm", ".rmvb", ".divx", ".f4v", ".evo")
