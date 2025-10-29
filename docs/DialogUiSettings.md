# ui2.stg

File is an incremental listing of text descriptions and their values in an indexed length-prefixed record.

For example:

```bash
01 00 00 00 2C 00 00 00 00 00 00 00 0C 00 00 00 ....,...........
64 65 73 6B 74 6F 70 5F 72 65 63 74 01 00 00 00 desktop_rect....
10 00 00 00 00 00 00 00 19 00 00 00 80 07 00 00 ............€...
3B 04 00 00                                     ;...
```

In this example, `01 00 00 00` is the little-endian index number (1) for the data block containing the title and data.  
The next 4 bytes `2C 00 00 00` is the length of the data block for index 1 (44).
The next 4 bytes `00 00 00 00` is the first index number within this data block (0).
The next 4 bytes `0C 00 00 00` is the length of the title of the data within this data block (12).
The next 12 bytes (title length), is the utf-8 title (desktop_rect) for the data in this data block.
The next 4 bytes `01 00 00 00` is the second index number of this data block (1).
The next 4 bytes `10 00 00 00` is the length of the data within this data block (16).
The next 16 bytes (data length), is the data for this data block.

So in this instance, this block displays the desktop resolution (i.e. rect or rectangle).  
More will be explained later about where this definition and others are found.

```bash
favorites_pinned_on_start - bool - gui/main_window/contact_list/FavoritesUtils.cpp
available_geometry - 4 doubles (4 byte values) (last two are witdth and height) for width and height - gui/main_window/MainWindow.cpp 2419
desktop_rect - 4 doubles as from available_geometry
download_directory_save_as - text - but unsure of values which follow
first_run - bool - gui/main_window/MainWindow.cpp 3292
keep_logged_in - bool - gui/main_window/LoginPage.cpp 1368
language - text 
last_version - text - gui/main_window/MainWindow.cpp
local_pin_timeout - int - gui/main_window/LocalPIN.cpp
login_page_last_entered_phone - text - gui/core_dispatcher.cpp
login_page_last_entered_uin - text - gui/core_dispatcher.cpp
login_page_last_login_type - int - core/connections/im_login.h 0: password, 1: phone, 2: Oauth2
login_page_need_fill_profile - bool - gui/core_dispatcher.cpp
mac_accounts_migrated - bool
main_window_rect - as per available_geometry and desktop_rect
microphone - text
mplayer_volume - int32 - gui/main_window/mplayer/VideoPlayer.cpp
pinned_chats_visible
recents_emojis_v2 - text gui/main_window/smiles_menu/SmilesMenu.cpp
recents_emojis_v3 - text gui/main_window/smiles_menu/SmilesMenu.cpp
recents_mini_mode
release_notes_sha1 - text
speakers - text
splitter_state
splitter_state_scale
stat_last_posted_times - multiple int64 values - gui/utils/periodic_gui_metrics.cpp
timestamps in bigendian, 8 bytes, unix milli 6 bytes of ts and 2 00 00. 8 byte event be from corelib enum
statuses_user_statuses - string
user_download_directory - text 
upload_directory - text
webcam - text
window_maximized - bool
```
