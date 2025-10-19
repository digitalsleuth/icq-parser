
# ICQ Data Folders Breakdown

## Desktop

### ICQ Folder

`logs`: Files containing network logs for activity for the app  

`settings`: 4 files, ending with .stg extensions, named core, ims, omicron, and ui2.

- `core.stg` is a binary file which follows a similar structure to the _db files. A 4 byte index, a 4 byte size of data to follow, then the data. It is the Core Settings file.
- `ims.stg` follows the same structure as `core`.
- `omicron.stg` is a high-entropy binary file. It is the Omicron Cache file.
- `ui2.stg` contains basic UI configuration settings, including some basic user details (upload path, last posted, connected AV devices)

`stats`: 3 files, im_stats, stats, and stats_mt .stg

- `im_stats.stg` is a binary file which has app specific details
- `stats.stg` shows basic details of the system including OS, RAM, Video Capture, upload/download "count", and a "hashed_user_key" value.
- `stats_mt.stg` has been empty in all samples so far.

`themes`: Wallpapers and customizations for chat backgrounds

`app.ini`: App configuration information

`vplog.dat`: Samples have all been 2-byte files.  

`zdicts`: Empty folder in all samples so far

### 0001 Folder

`archive`: Folders for each aimId, containing files named `_db*`, `_idx*`, `_ste*`, `_gc*`, `_gs*`, `_mentions`, `_reactions_db`, `_reactions_idx`  

`avatars`: Folders for each aimId and a file named ceilbigbuddyicon_.jpg in each. Some also have a floorbigbuddyicon_.jpg as well, or a file named likely for the resolution (128.jpg etc).  

`contacts`: Has a folder named cache.cl which is a JSON file containing basic contact info for each aimId

`content.cache`: Files that have been sent or received

`Dialogs`: Has a file named `cache2` which appears to be a JSON list of aimIds, but no indication of what they're specifically for at this time

`favorites`: Contains a file named `cache2` which is a JSON file containing users which are favorites. It includes the aimId, time they were favorited, their friendly name, and whether they are "Official" (ie a validated, official ICQ user)

`info`: Has a file named "cache" which is a breakdown of the core users profile. Has DisplayName, NickName, ID, status, user type, and phone number (where applicable)

`key`: Has two files -

- `fetch` Appears to be the means by which the application retrieves events from the online service (likely not user interaction, done in behind). Seems to contain a timestamp in the URL in the file as well.

- `value.au` is a high-entropy file, 352 bytes. No apparent logical structure. Is referred to as an Auth file.

`masks`: Appears to be user profile picture themes.

`search`: Contains folders with uids, and in each a file named "hst". Looks simply like a text file containing words searched for in the chat with that user. Not always present.

`stickers`: Similar to emoji or icons which can be shared or used in a chat

## iOS

Coming Soon


### Notes

Files with audio_message and a hex value at the end: The hex value is a 12 character hex unix millisecond timestamp, without the leading 0
ex: `audio_message199f7b609aa`.m4a

### References

[ICQ Desktop source repository](https://github.com/mail-ru-im/im-desktop/) - [https://github.com/mail-ru-im/im-desktop/](https://github.com/mail-ru-im/im-desktop/)

[ICQ Desktop (deprecated version)](https://github.com/mailru/icqdesktop.deprecated) - [https://github.com/mailru/icqdesktop.deprecated](https://github.com/mailru/icqdesktop.deprecated)
