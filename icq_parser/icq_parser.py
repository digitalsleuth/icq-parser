#!/usr/bin/env python3

import sqlite3
import os
import sys
import json
import argparse
import plistlib
import ipaddress
import threading
import time
import subprocess
import struct
import logging
from pathlib import Path
from datetime import datetime as dt, timezone, timedelta
from dataclasses import dataclass
from string import hexdigits
import requests
from flask import send_from_directory
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from pypdf import PdfWriter
from pywebcopy import save_website
import magic

try:
    from icq_parser import icqweb
    from icq_parser.icqweb import build_search_index
except ImportError:
    import icqweb
    from icqweb import build_search_index

## TODO: Log Parsing - | grep ^{ | awk -F curl '{print $1}' | grep -v '{"method' | less
## TODO: Review im-desktop/core/Voip/libvoip/include/voip/voip3.h - ToPackedString

__fmt__ = "%Y-%m-%d %H:%M:%S"
ASCII_MAX = 128
INDEX_DIVISOR = 62
CHARSET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
PDFS = []


def get_reverse_index_map():
    # Maps ASCII char to index 0..61, like a manual Base62 conversion
    map_ = [-1] * ASCII_MAX
    index = 0
    for ch in range(ord("0"), ord("9") + 1):
        map_[ch] = index
        index += 1
    for ch in range(ord("a"), ord("z") + 1):
        map_[ch] = index
        index += 1
    for ch in range(ord("A"), ord("Z") + 1):
        map_[ch] = index
        index += 1
    return map_


REVERSE_INDEX_MAP = get_reverse_index_map()


@dataclass
class FileSharingIdInfo:
    file_type: str
    unique_value_one: str
    unique_value_two: str
    timestamp: str


@dataclass
class FileSharingContentType:
    type_: str

    def is_ptt(self):
        return self.type_ == "ptt"

    def is_lottie(self):
        return self.type_ == "lottie"

    def is_video(self):
        return self.type_ == "video"

    def is_image(self):
        return self.type_ in {"gif", "gif-sticker", "image", "image-sticker"}


class FileSharingUriParser:
    """
    Information for how this is used is found in
    im-desktop/gui/main_window/history_control/complex_message/FileSharingUtils.cpp
    and im-desktop/common.shared/constants.h
    """

    def __init__(self, file_id: str):
        if file_id[:4] in {"hxxp", "http"}:
            file_id = file_id.split("/")[-1]
        if len(file_id) < 30:
            raise ValueError(f"[!] Invalid file-sharing URI length: {len(file_id)}")
        self.file_id = file_id
        self.file_type = file_id[0]
        self.unique_value_one = file_id[5:22]
        self.timestamp = file_id[22:30]
        self.unique_value_two = file_id[30:]

    def extract_content_type(self) -> FileSharingContentType:
        c = self.file_type
        if c == "4":
            t = "gif"
        elif c == "5":
            t = "gif-sticker"
        elif c in "IJ":
            t = "ptt"
        elif c == "L":
            t = "lottie-sticker"
        elif c == "S":
            t = "pdf"
        elif c in "0134567":
            t = "image"
        elif c == "2":
            t = "image-sticker"
        elif c in "89ABCEF":
            t = "video"
        elif c == "D":
            t = "video-sticker"
        else:
            t = "unknown"
        return FileSharingContentType(type_=t)

    def decode_duration(self) -> int:
        """
        Length of PTT files uses characters 1-4, base62 encoded, as duration.
        """
        if len(self.file_id) < 5:
            return None
        part = self.file_id[1:5]
        duration = 0
        for i, ch in enumerate(part):
            value = CHARSET.index(ch)
            exp = len(CHARSET) * (len(part) - i - 1)
            if exp > 0:
                value *= exp
            duration += value
        return duration

    def decode_size(self):
        """
        Image width is chars 1 and 2, height is 3 and 4.
        All are base62 encoded.
        """
        if len(self.file_id) < 5:
            return None

        def decode_pair(ch0, ch1):
            index0 = REVERSE_INDEX_MAP[ord(ch0)]
            index1 = REVERSE_INDEX_MAP[ord(ch1)]
            return index0 * INDEX_DIVISOR + index1

        width = decode_pair(self.file_id[1], self.file_id[2])
        height = decode_pair(self.file_id[3], self.file_id[4])
        return width, height

    def decode_timestamp(self) -> str:
        dt_val = self.timestamp
        if not len(dt_val) == 8 or not all(char in hexdigits for char in dt_val):
            return None
        to_dec = int(dt_val, 16)
        try:
            ts = dt.fromtimestamp(float(to_dec), timezone.utc).strftime(__fmt__)
        except (OSError, OverflowError, ValueError):
            return None
        return ts


class DesktopParser:
    # im-desktop/common.shared/constants.h
    def __init__(self, start_path):
        folder_path = Path(start_path)
        self.folder_path = folder_path
        self.TS = 0
        self.msg = None
        self.uid = None
        self.OWNER_UID = None
        self.MESSAGE_ID = 0
        self.MESSAGES = {}
        self.AVATARS = {}
        self.CALL_LOG_CACHE = []
        self.CALL_LOG = {}
        self.CONTACT_LIST = {}
        self.CONTACT_LIST_CACHE = []
        self.DB_FILES = {}
        self.DIALOGS = []
        self.DIALOGS_FILES = []
        self.DIALOG_STATES = {}
        self.DIALOG_STATE_FILES = {}
        self.DRAFT_FILES = {}
        self.DRAFTS = {}
        self.GALLERY_CACHE_FILES = {}
        self.GALLERY_STATE_FILES = {}
        self.GALLERY_STATE = {}
        self.HISTORY_FILES = {}
        self.SEARCH_HISTORY = {}
        self.INFO_CACHE = {}
        self.INFO_CACHE_FILES = []
        self.PARK = {}
        self.RAW_TIME = 0
        self.SHARED_FILES = {}
        self.DIRECTION = {True: "OUTGOING", False: "INCOMING"}
        self.VOIP_DIRECTION = {0: "OUTGOING", 1: "INCOMING"}
        self.VERBOSE = False
        # im-desktop/core/archive/gallery_cache.cpp
        self.MEDIA_TYPES = {
            "FILES": 0,
            "IMAGES": 0,
            "LINKS": 0,
            "PTT": 0,
            "VIDEOS": 0,
            "OTHER": 0,
        }
        self.STRUCT_DICT = {
            1: "<B",
            2: "<H",
            4: "<I",
            8: "<II",
            12: "<III",
            16: "<IIII",
        }
        # im-desktop/corelib/enumerations.h - voip_event_type
        self.VOIP_EVENT = {
            0: "invalid",
            1: "min",
            2: "missed call",
            3: "call ended",
            4: "call accepted",
            5: "call declined",
            6: "max",
        }
        # im-desktop/corelib/enumerations.h - chat_event_type
        self.CHAT_EVENT = {
            0: "invalid",
            1: "min",
            2: "added to buddy list",
            3: "add members to chat",
            4: "invite",
            5: "leave",
            6: "delete members from chat",
            7: "kicked",
            8: "chat name modified",
            9: "buddy registered",
            10: "buddy found",
            11: "birthday",
            12: "avatar modified",
            13: "generic",
            14: "chat description modified",
            15: "message deleted",
            16: "chat rules modified",
            17: "chat stamp modified",
            18: "chat join moderation modified",
            19: "chat public modified",
            20: "chat trust required modified",
            21: "chat threads enabled modified",
            22: "mchat admin granted",
            23: "mchat admin revoked",
            24: "mchat allowed to write",
            25: "mchat disallowed to write",
            26: "mchat waiting for approval",
            27: "mchat joining approved",
            28: "mchat joining rejected",
            29: "mchat joining canceled",
            30: "warn about stranger",
            31: "no longer stranger",
            32: "status reply",
            33: "custom status reply",
            34: "task changed",
            35: "max",
        }
        self.BLANK_CONTACT = {
            "AbContactName": None,
            "AbPhoneNumber": None,
            "AbPhones": None,
            "SMSNumber": None,
            "CellNumber": None,
            "PhoneNumber": None,
            "WorkNumber": None,
            "OtherNumber": None,
            "AIMID": None,
            "AutoAddition": None,
            "AvatarCeilBig": None,
            "AvatarFloorBig": None,
            "AvatarFloorLarge": None,
            "AvatarOther": None,
            "Blocked": None,
            "Bot": None,
            "Capabilities": None,
            "Deleted": None,
            "DisplayName": None,
            "FirstName": None,
            "FriendlyName": None,
            "GroupId": None,
            "Ignored": None,
            "LastName": None,
            "LastSeen": None,
            "LiveChat": None,
            "MessagesReceived": 0,
            "MessagesSent": 0,
            "MessagesTotal": 0,
            "MoodIcon": None,
            "Muted": None,
            "NickName": None,
            "Official": None,
            "OfflineMessage": None,
            "OutgoingCount": None,
            "ReadOnly": None,
            "SSL": None,
            "State": None,
            "StatusMessage": None,
            "UID": None,
            "UserType": None,
        }
        # identified through log files and data comparison within iOS ICQ databases
        self.USER_TYPE = {2: "icq", 3: "interop", 4: "sms", 5: "chat"}

        for file in folder_path.rglob("*"):
            if file.match("_db*") and file.parent.name in self.DB_FILES:
                self.DB_FILES[file.parent.name].append(str(file))
            elif file.match("_db*"):
                self.DB_FILES[file.parent.name] = [str(file)]
            elif file.match("_gc*") and file.parent.name in self.GALLERY_CACHE_FILES:
                self.GALLERY_CACHE_FILES[file.parent.name].append(str(file))
            elif file.match("_gc*"):
                self.GALLERY_CACHE_FILES[file.parent.name] = [str(file)]
            elif file.match("_gs*") and file.parent.name in self.GALLERY_STATE_FILES:
                self.GALLERY_STATE_FILES[file.parent.name].append(str(file))
            elif file.match("_gs*"):
                self.GALLERY_STATE_FILES[file.parent.name] = [str(file)]
            elif file.match("_ste*") and file.parent.name in self.DIALOG_STATE_FILES:
                self.DIALOG_STATE_FILES[file.parent.name].append(str(file))
            elif file.match("_ste*"):
                self.DIALOG_STATE_FILES[file.parent.name] = [str(file)]
            elif file.match("_draft*") and file.parent.name in self.DRAFT_FILES:
                self.DRAFT_FILES[file.parent.name].append(str(file))
            elif file.match("_draft*"):
                self.DRAFT_FILES[file.parent.name] = [str(file)]
            elif file.match("hst") and file.parent.name in self.HISTORY_FILES:
                self.HISTORY_FILES[file.parent.name].append(str(file))
            elif file.match("hst"):
                self.HISTORY_FILES[file.parent.name] = [str(file)]
            elif file.match("cache") and file.parent.name == "info":
                self.INFO_CACHE_FILES.append(str(file))
            elif file.match("cache.cl"):
                self.CONTACT_LIST_CACHE.append(str(file))
            elif file.match("cache*") and file.parent.name == "dialogs":
                self.DIALOGS_FILES.append(str(file))
            elif file.match("avatars") and os.path.isdir(file):
                avatar_path = Path(file)
                for img in avatar_path.rglob("*.jpg"):
                    if img.parent.name in self.AVATARS:
                        self.AVATARS[img.parent.name].append(str(img))
                    else:
                        self.AVATARS[img.parent.name] = [str(img)]
            elif file.match("call_log.cache"):
                self.CALL_LOG_CACHE.append(str(file))

    def get_db_content(self):
        if not self.DB_FILES:
            return None
        for uid, files in self.DB_FILES.items():
            self.uid = uid
            if uid not in self.MESSAGES:
                self.MESSAGES[uid] = {}
                self.PARK[uid] = {}
            for filename in files:
                with open(filename, "rb") as file:
                    content = file.read()
                    if len(content) == 0:
                        return None
                while len(content) >= 16:
                    blk_size, blk_chk = struct.unpack_from("<II", content, 0)
                    if blk_size != blk_chk:
                        break
                    offset = 0
                    self.msg = None
                    blk_end = blk_size + 8
                    if blk_end + 8 > len(content):
                        break
                    blk = memoryview(content)[8:blk_end]
                    while offset + 8 <= len(blk):
                        chunk = struct.unpack_from("<II", blk, offset)
                        handler_id = chunk[0]
                        if handler_id in handlers:
                            if handler_id == 1:
                                value, offset = handlers[handler_id][0](
                                    self, chunk, blk, offset
                                )
                                self.MESSAGE_ID = value
                                if self.MESSAGE_ID not in self.PARK[self.uid]:
                                    self.PARK[self.uid][self.MESSAGE_ID] = {
                                        "MESSAGE": {},
                                        "UID": self.uid,
                                    }
                                    if self.msg is not None:
                                        self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"][
                                            "TEXT"
                                        ] = self.msg
                                elif (
                                    self.MESSAGE_ID in self.PARK[self.uid]
                                    and "TEXT"
                                    in self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"]
                                    and self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"][
                                        "TEXT"
                                    ]
                                    == self.msg
                                ):
                                    pass
                                elif (
                                    self.MESSAGE_ID in self.PARK[self.uid]
                                    and "TEXT"
                                    in self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"]
                                    and self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"][
                                        "TEXT"
                                    ]
                                    is not None
                                ):
                                    self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"][
                                        "TEXT"
                                    ] += f"\n{self.msg}"
                                elif (
                                    self.MESSAGE_ID in self.PARK[self.uid]
                                    and "TEXT"
                                    not in self.PARK[self.uid][self.MESSAGE_ID][
                                        "MESSAGE"
                                    ]
                                ):
                                    self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"][
                                        "TEXT"
                                    ] = self.msg
                                if self.msg == "Message was deleted":
                                    self.PARK[self.uid][self.MESSAGE_ID]["MESSAGE"][
                                        "DELETED"
                                    ] = True
                                self.msg = None
                            else:
                                value, offset = handlers[handler_id][0](
                                    self, chunk, blk, offset
                                )
                            key = handlers[handler_id][1]
                            dest = handlers[handler_id][2]
                            if dest is None:
                                continue
                            if not self.VERBOSE and value == "":  ##
                                continue
                            if (
                                dest == "VOIP"
                                and "VOIP" not in self.PARK[uid][self.MESSAGE_ID]
                            ):
                                self.PARK[uid][self.MESSAGE_ID][dest] = {}
                            if handler_id == 13:
                                key = f"{key}{uid}"
                                if (
                                    value is None
                                    and key in self.PARK[uid][self.MESSAGE_ID][dest]
                                    and self.PARK[uid][self.MESSAGE_ID][dest][key]
                                    is not None
                                ):
                                    value = self.PARK[uid][self.MESSAGE_ID][dest][key]

                            if handler_id == 2:
                                self.PARK[uid][self.MESSAGE_ID][dest]["DIRECTION"] = (
                                    self.DIRECTION[value["OUTGOING"]]
                                )
                            if handler_id == 3 and self.RAW_TIME != 0:
                                self.PARK[uid][self.MESSAGE_ID][dest][
                                    "TIME_RAW"
                                ] = self.RAW_TIME
                            elif handler_id == 3 and self.RAW_TIME == 0:
                                if (
                                    self.MESSAGE_ID in self.PARK[uid]
                                    and "Message was deleted"
                                    in self.PARK[uid][self.MESSAGE_ID]["MESSAGE"][
                                        "TEXT"
                                    ]
                                ):
                                    if (
                                        self.PARK[uid][self.MESSAGE_ID]["MESSAGE"][
                                            "TIME"
                                        ]
                                        is not None
                                        and value is None
                                    ):
                                        value = self.PARK[uid][self.MESSAGE_ID][
                                            "MESSAGE"
                                        ]["TIME"]
                            self.PARK[uid][self.MESSAGE_ID][dest][key] = value
                            self.RAW_TIME = 0
                        else:
                            offset += 8 + chunk[1]
                    content = content[blk_end + 8 :]
                    self.MESSAGES[self.uid] = self.PARK[self.uid]
        return self.MESSAGES

    def get_info_cache(self):
        if not self.INFO_CACHE_FILES:
            return None
        for file in self.INFO_CACHE_FILES:
            try:
                file_type = magic.from_file(file)
            except ValueError:
                return None
            if file_type.startswith("ASCII"):
                with open(file, encoding="utf-8") as f:
                    json_data = json.load(f)
                    self.INFO_CACHE["NICKNAME"] = json_data["info"]["nick"]
                    self.OWNER_UID = self.INFO_CACHE["AIMID"] = self.INFO_CACHE[
                        "UID"
                    ] = json_data["info"]["aimId"]
                    self.INFO_CACHE["FRIENDLY_NAME"] = json_data["info"]["friendly"]
                    self.INFO_CACHE["STATE"] = json_data["info"]["state"]
                    self.INFO_CACHE["USER_TYPE"] = json_data["info"]["userType"]
                    self.INFO_CACHE["ATTACHED_PHONE_NUMBER"] = json_data["info"][
                        "attachedPhoneNumber"
                    ]
                    self.INFO_CACHE["GLOBAL_FLAGS"] = json_data["info"]["globalFlags"]
                    self.INFO_CACHE["HAS_MAIL"] = json_data["info"]["hasMail"]
                    self.INFO_CACHE["ACCOUNT_IS_OFFICIAL"] = json_data["info"][
                        "official"
                    ]
            else:
                with open(file, "rb") as f:
                    content = f.read()
                    blk_size, blk_chk = struct.unpack_from("<II", content, 0)
                    if blk_size != blk_chk:
                        return None
                    offset = 8
                    blk_end = blk_size + 8
                    blk = memoryview(content)[:blk_end]
                    while offset + 8 <= blk_end:
                        chunk = struct.unpack_from("<II", blk, offset)
                        handler_id = chunk[0]
                        if handler_id in my_info_handlers:
                            value, offset = my_info_handlers[handler_id][0](
                                self, chunk, blk, offset
                            )
                            key = my_info_handlers[handler_id][1]
                            self.INFO_CACHE[key] = value
                        else:
                            offset += 8
                    content = content[blk_size + 8 :]
                self.OWNER_UID = self.INFO_CACHE["AIMID"] if not None else None
        return self.INFO_CACHE

    def get_contact_list(self):
        if not self.CONTACT_LIST_CACHE:
            return None
        for file in self.CONTACT_LIST_CACHE:
            try:
                file_type = magic.from_file(file)
            except ValueError:
                return None
            if file_type.startswith("ASCII") or file_type.startswith("UTF-8"):
                with open(file, encoding="utf-8") as f:
                    json_data = json.load(f)
                for group in json_data["groups"]:
                    for buddy in group["buddies"]:
                        b_id = buddy["aimId"]
                        self.CONTACT_LIST[b_id] = self.BLANK_CONTACT.copy()
                        self.CONTACT_LIST[b_id]["AIMID"] = b_id
                        self.CONTACT_LIST[b_id]["UID"] = b_id
                        self.CONTACT_LIST[b_id]["AbContactName"] = buddy.get(
                            "abContactName", None
                        )
                        self.CONTACT_LIST[b_id]["AbPhoneNumber"] = buddy.get(
                            "abPhoneNumber", None
                        )
                        self.CONTACT_LIST[b_id]["AbPhones"] = buddy.get(
                            "abPhones", None
                        )
                        self.CONTACT_LIST[b_id]["SMSNumber"] = buddy.get(
                            "smsNumber", None
                        )
                        self.CONTACT_LIST[b_id]["CellNumber"] = buddy.get(
                            "cellNumber", None
                        )
                        self.CONTACT_LIST[b_id]["PhoneNumber"] = buddy.get(
                            "phoneNumber", None
                        )
                        self.CONTACT_LIST[b_id]["WorkNumber"] = buddy.get(
                            "workNumber", None
                        )
                        self.CONTACT_LIST[b_id]["OtherNumber"] = buddy.get(
                            "otherNumber", None
                        )
                        self.CONTACT_LIST[b_id]["AutoAddition"] = buddy.get(
                            "autoAddition", None
                        )
                        if b_id in self.AVATARS:
                            avatars = self.AVATARS[b_id]
                            for avatar in avatars:
                                if "ceilbig" in avatar:
                                    self.CONTACT_LIST[b_id]["AvatarCeilBig"] = avatar
                                elif "floorbig" in avatar:
                                    self.CONTACT_LIST[b_id]["AvatarFloorBig"] = avatar
                                elif "floorlarge" in avatar:
                                    self.CONTACT_LIST[b_id]["AvatarFloorLarge"] = avatar
                                else:
                                    self.CONTACT_LIST[b_id]["AvatarOther"] = avatar
                        self.CONTACT_LIST[b_id]["Blocked"] = buddy.get("blocked", None)
                        self.CONTACT_LIST[b_id]["Bot"] = buddy.get("bot", None)
                        self.CONTACT_LIST[b_id]["Capabilities"] = buddy.get(
                            "capabilities", None
                        )
                        if "@chat.agent" in b_id:
                            self.CONTACT_LIST[b_id]["ConversationType"] = "GROUP CHAT"
                        else:
                            self.CONTACT_LIST[b_id]["ConversationType"] = "PRIVATE"
                        self.CONTACT_LIST[b_id]["Deleted"] = buddy.get("deleted", None)
                        self.CONTACT_LIST[b_id]["DisplayName"] = buddy.get(
                            "displayId", None
                        )
                        self.CONTACT_LIST[b_id]["FirstName"] = buddy.get(
                            "firstName", None
                        )
                        self.CONTACT_LIST[b_id]["LastName"] = buddy.get(
                            "lastName", None
                        )
                        self.CONTACT_LIST[b_id]["FriendlyName"] = buddy.get(
                            "friendly", None
                        )
                        self.CONTACT_LIST[b_id]["GroupName"] = group["name"]
                        self.CONTACT_LIST[b_id]["Ignored"] = buddy.get("ignored", None)
                        if "userState" in buddy:
                            raw = buddy["userState"]["lastseen"]
                            if buddy["userState"]["lastseen"] == -1:
                                last_seen = "Not seen on ICQ"
                            else:
                                last_seen = convert_unix_ts(
                                    buddy["userState"]["lastseen"]
                                )
                            self.CONTACT_LIST[b_id]["LastSeen"] = last_seen
                            self.CONTACT_LIST[b_id]["LastSeenRaw"] = raw
                        elif "lastseen" in buddy:
                            raw = buddy["lastseen"]
                            if buddy["lastseen"] == -1:
                                last_seen = "Not seen on ICQ"
                            else:
                                last_seen = convert_unix_ts(buddy["lastseen"])
                            self.CONTACT_LIST[b_id]["LastSeen"] = last_seen
                            self.CONTACT_LIST[b_id]["LastSeenRaw"] = raw
                        self.CONTACT_LIST[b_id]["LiveChat"] = bool(
                            buddy.get("livechat", None)
                        )
                        self.CONTACT_LIST[b_id]["MoodIcon"] = buddy.get(
                            "moodIcon", None
                        )
                        self.CONTACT_LIST[b_id]["Muted"] = buddy.get("mute", None)
                        self.CONTACT_LIST[b_id]["NickName"] = buddy.get("nick", "")
                        self.CONTACT_LIST[b_id]["Official"] = bool(
                            buddy.get("official", None)
                        )
                        self.CONTACT_LIST[b_id]["IconId"] = buddy.get("iconId", None)
                        self.CONTACT_LIST[b_id]["BigIconId"] = buddy.get(
                            "bigIconId", None
                        )
                        self.CONTACT_LIST[b_id]["LargeIconId"] = buddy.get(
                            "largeIconId", None
                        )
                        self.CONTACT_LIST[b_id]["PublicChat"] = bool(
                            buddy.get("public", None)
                        )
                        self.CONTACT_LIST[b_id]["OfflineMessage"] = buddy.get(
                            "offlineMsg", None
                        )
                        self.CONTACT_LIST[b_id]["OutgoingCount"] = buddy.get(
                            "outgoingCount", None
                        )
                        self.CONTACT_LIST[b_id]["ReadOnly"] = buddy.get(
                            "readOnly", None
                        )
                        self.CONTACT_LIST[b_id]["SSL"] = buddy.get("ssl", None)
                        if "state" in buddy:
                            self.CONTACT_LIST[b_id]["State"] = buddy.get("state", None)
                        self.CONTACT_LIST[b_id]["StatusMessage"] = buddy.get(
                            "statusMsg", None
                        )
                        self.CONTACT_LIST[b_id]["Status"] = buddy.get("status", None)
                        self.CONTACT_LIST[b_id]["UserType"] = buddy.get(
                            "userType", None
                        )
                if "ignorelist" in json_data:
                    self.CONTACT_LIST["IgnoreList"] = json_data["ignorelist"]
        return self.CONTACT_LIST

    def get_call_log(self):
        if not self.CALL_LOG_CACHE:
            return None
        for file in self.CALL_LOG_CACHE:
            with open(file, "rb") as cache_file:
                content = cache_file.read()
                if len(content) == 0:
                    continue
            while len(content) >= 16:
                blk_size, blk_chk = struct.unpack_from("<II", content, 0)
                if blk_size != blk_chk:
                    break
                offset = 0
                blk_end = blk_size + 8
                if blk_end + 8 > len(content):
                    break
                blk = memoryview(content)[8:blk_end]
                while offset + 8 <= len(blk):
                    chunk = struct.unpack_from("<II", blk, offset)
                    handler_id = chunk[0]
                    if handler_id in handlers:
                        value, offset = handlers[handler_id][0](
                            self, chunk, blk, offset
                        )
                        key = handlers[handler_id][1]
                        dest = handlers[handler_id][2]
                        if dest is None:
                            continue
                        dest = f"CALL_LOG_{key}"
                        if not self.VERBOSE and value == "":  ##
                            continue
                        if self.MESSAGE_ID not in self.CALL_LOG:
                            self.CALL_LOG[self.MESSAGE_ID] = {}
                        self.CALL_LOG[self.MESSAGE_ID][dest] = {}
                        self.CALL_LOG[self.MESSAGE_ID][dest][key] = value
                        if handler_id == 2:
                            self.CALL_LOG[self.MESSAGE_ID][dest]["DIRECTION"] = (
                                self.DIRECTION[value["OUTGOING"]]
                            )
                    else:
                        offset += 8 + chunk[1]
                content = content[blk_end + 8 :]
        return self.CALL_LOG

    def get_dialogs(self):
        if not self.DIALOGS_FILES:
            return None
        for file in self.DIALOGS_FILES:
            try:
                file_type = magic.from_file(file)
            except ValueError:
                return None
            if file_type.startswith("ASCII") or file_type.startswith("UTF-8"):
                with open(file, encoding="utf-8") as f:
                    json_data = json.load(f)
                for aimid in json_data["dialogs"]:
                    self.DIALOGS.append(aimid["aimId"])
        return self.DIALOGS

    def get_shared_files(self):
        MEDIA_TYPES = {
            "FILES": 0,
            "IMAGES": 0,
            "LINKS": 0,
            "PTT": 0,
            "VIDEOS": 0,
            "OTHER": 0,
        }
        MSG_ID = None
        if not self.GALLERY_CACHE_FILES:
            return None
        for uid, file_paths in self.GALLERY_CACHE_FILES.items():
            for file_path in file_paths:
                with open(file_path, "rb") as file:
                    content = file.read()
                    if len(content) == 0:
                        continue
                    if uid not in self.SHARED_FILES:
                        self.SHARED_FILES[uid] = {}
                    while len(content) >= 16:
                        blk_size, blk_chk = struct.unpack_from("<II", content, 0)
                        if blk_size != blk_chk:
                            break
                        offset = 0
                        blk_end = blk_size + 8
                        if blk_end + 8 > len(content):
                            break
                        blk = memoryview(content)[8:blk_end]
                        while offset + 8 <= len(blk):
                            chunk = struct.unpack_from("<II", blk, offset)
                            handler_id = chunk[0]
                            if handler_id in shared_files_handlers:
                                value, offset = shared_files_handlers[handler_id][0](
                                    self, chunk, blk, offset
                                )
                                key = shared_files_handlers[handler_id][1]
                                dest = shared_files_handlers[handler_id][2]
                                if dest is None:
                                    continue
                                if not self.VERBOSE and value == "":  ##
                                    continue
                                if handler_id == 2:
                                    MSG_ID = value
                                if handler_id == 6 and "hxxps://files.icq.net" in value:
                                    file_metadata = {}
                                    file_id = value.split("/")[-1]
                                    file_type, file_timestamp, file_size = (
                                        parse_file_id(file_id)
                                    )
                                    file_metadata["URI_DECODED_CONTENT_TYPE"] = (
                                        file_type
                                    )
                                    file_metadata["URI_DECODED_CONTENT_TIMESTAMP"] = (
                                        file_timestamp
                                    )
                                    file_metadata["URI_DECODED_CONTENT_SIZE"] = (
                                        file_size
                                    )
                                    self.SHARED_FILES[uid][MSG_ID][
                                        "URI_DECODED_METADATA"
                                    ] = file_metadata
                                    self.SHARED_FILES[uid][MSG_ID][key] = value
                                if handler_id == 9:
                                    self.SHARED_FILES[uid][MSG_ID]["DIRECTION"] = (
                                        self.DIRECTION[value["OUTGOING"]]
                                    )
                                if MSG_ID not in self.SHARED_FILES[uid]:
                                    self.SHARED_FILES[uid][MSG_ID] = {}
                                self.SHARED_FILES[uid][MSG_ID][key] = value
                            else:
                                offset += 8 + chunk[1]
                        content = content[blk_end + 8 :]
        return self.SHARED_FILES

    def get_drafts(self):
        if not self.DRAFT_FILES:
            return None
        for uid, file_paths in self.DRAFT_FILES.items():
            for file_path in file_paths:
                with open(file_path, "rb") as file:
                    content = file.read()
                    offset = 0
                    len_content = len(content)
                    if len(content) == 0:
                        continue
                    if uid not in self.DRAFTS:
                        self.DRAFTS[uid] = {}
                    while offset < len_content:
                        blk = memoryview(content)[:len_content]
                        while offset < len_content:
                            chunk = struct.unpack_from("<II", blk, offset)
                            handler_id = chunk[0]
                            if handler_id in draft_files_handlers:
                                if handler_id == 2:
                                    value, offset = draft_files_handlers[handler_id][0](
                                        self, chunk, blk, offset, update_ts=True
                                    )
                                else:
                                    value, offset = draft_files_handlers[handler_id][0](
                                        self, chunk, blk, offset
                                    )
                                key = draft_files_handlers[handler_id][1]
                                dest = draft_files_handlers[handler_id][2]
                                if handler_id == 1:
                                    self.PARK[key] = value
                                    continue
                                if dest is None:
                                    continue
                                if not self.VERBOSE and value == "":  ##
                                    continue
                                if self.TS not in self.DRAFTS[uid]:
                                    self.DRAFTS[uid][self.TS] = {}
                                self.DRAFTS[uid][self.TS][key] = value
                                if handler_id == 2:
                                    for k, v in self.PARK.items():
                                        self.DRAFTS[uid][self.TS][k] = v
                                if handler_id == 3:
                                    while offset < len_content:
                                        chunk = struct.unpack_from("<II", blk, offset)
                                        handler_id = chunk[0]
                                        if handler_id in handlers:
                                            value, offset = handlers[handler_id][0](
                                                self, chunk, blk, offset
                                            )
                                            key = handlers[handler_id][1]
                                            dest = handlers[handler_id][2]
                                            if key == "PREVIOUS_MESSAGE_ID_WITH_":
                                                key = "DRAFT_PREVIOUS_MESSAGE_ID"
                                            if dest is None:
                                                continue
                                            dest = f"DRAFT_{handlers[handler_id][2]}"
                                            if not self.VERBOSE and value == "":  ##
                                                continue
                                            self.DRAFTS[uid][self.TS][key] = value
                            else:
                                offset += 8 + chunk[1]
                        content = content[len_content + 8 :]
        return self.DRAFTS

    def get_msg_search_history(self):
        if not self.HISTORY_FILES:
            return None
        for uid, file_paths in self.HISTORY_FILES.items():
            if uid not in self.SEARCH_HISTORY:
                self.SEARCH_HISTORY[uid] = []
            for file_path in file_paths:
                with open(file_path, "r", encoding="utf-8") as hst_file:
                    for line in hst_file.readlines():
                        self.SEARCH_HISTORY[uid].append(str(line.rstrip()))
        return self.SEARCH_HISTORY

    def get_gallery_state(self):
        if not self.GALLERY_STATE_FILES:
            return None
        for uid, files in self.GALLERY_STATE_FILES.items():
            if uid not in self.GALLERY_STATE:
                self.GALLERY_STATE[uid] = {}
            for filename in files:
                with open(filename, "rb") as file:
                    content = file.read()
                    if len(content) == 0:
                        return None
                while len(content) >= 16:
                    blk_size, blk_chk = struct.unpack_from("<II", content, 0)
                    if blk_size != blk_chk:
                        break
                    offset = 0
                    self.msg = None
                    blk_end = blk_size + 8
                    if blk_end + 8 > len(content):
                        break
                    blk = memoryview(content)[8:blk_end]
                    while offset + 8 <= len(blk):
                        chunk = struct.unpack_from("<II", blk, offset)
                        handler_id = chunk[0]
                        if handler_id in state_handlers:
                            value, offset = state_handlers[handler_id][0](
                                self, chunk, blk, offset
                            )
                            key = state_handlers[handler_id][1]
                            dest = state_handlers[handler_id][2]
                            if dest is None:
                                continue
                            if not self.VERBOSE and value == "":  ##
                                continue
                            self.GALLERY_STATE[uid][key] = value
                        else:
                            offset += 8 + chunk[1]
                    content = content[blk_end + 8 :]
        return self.GALLERY_STATE

    def get_dlg_state(self):
        # im-desktop/core/archive/dlg_state.cpp
        if not self.DIALOG_STATE_FILES:
            return None
        for uid, files in self.DIALOG_STATE_FILES.items():
            if uid not in self.DIALOG_STATES:
                self.DIALOG_STATES[uid] = {}
            for filename in files:
                with open(filename, "rb") as file:
                    content = file.read()
                    offset = 0
                    if len(content) == 0:
                        return None
                while len(content) >= 16:
                    blk_size, blk_chk = struct.unpack_from("<II", content, 0)
                    if blk_size != blk_chk:
                        break
                    self.msg = None
                    blk_end = blk_size + 8
                    if blk_end + 8 > len(content):
                        break
                    blk = memoryview(content)[8:blk_end]
                    while offset + 8 <= len(blk):
                        chunk = struct.unpack_from("<II", blk, offset)
                        handler_id = chunk[0]
                        if handler_id in dialog_state_handlers:
                            value, offset = dialog_state_handlers[handler_id][0](
                                self, chunk, blk, offset
                            )
                            key = dialog_state_handlers[handler_id][1]
                            dest = dialog_state_handlers[handler_id][2]
                            if dest is None:
                                continue
                            if not self.VERBOSE and value == "":  ##
                                continue
                            self.DIALOG_STATES[uid][key] = value
                            if handler_id == 7:
                                while offset + 8 <= len(blk):
                                    chunk = struct.unpack_from("<II", blk, offset)
                                    handler_id = chunk[0]
                                    if handler_id in handlers:
                                        value, offset = handlers[handler_id][0](
                                            self, chunk, blk, offset
                                        )
                                        key = handlers[handler_id][1]
                                        dest = handlers[handler_id][2]
                                        if handler_id == 2:
                                            self.DIALOG_STATES[uid]["DIRECTION"] = (
                                                self.DIRECTION[value["OUTGOING"]]
                                            )
                                        if handler_id == 5:
                                            dest = "TEXT"
                                        if dest is None:
                                            continue
                                        if handler_id == 13:
                                            key = f"{key}{uid}"
                                        if not self.VERBOSE and value == "":  ##
                                            continue
                                        self.DIALOG_STATES[uid][key] = value
                        else:
                            offset += 8 + chunk[1]
                    content = content[blk_end + 8 :]
        return self.DIALOG_STATES

    def correlate_data(self):
        for k, v in self.SHARED_FILES.items():
            num_shared_items = len(v)
            self.SHARED_FILES[k]["MediaTypes"] = self.MEDIA_TYPES.copy()
            for _, content in v.items():
                if "SHARED_CONTENT_TYPE" not in content:
                    continue
                value = content["SHARED_CONTENT_TYPE"]
                if value == "image":
                    self.SHARED_FILES[k]["MediaTypes"]["IMAGES"] += 1
                elif value == "video":
                    self.SHARED_FILES[k]["MediaTypes"]["VIDEOS"] += 1
                elif value == "file":
                    self.SHARED_FILES[k]["MediaTypes"]["FILES"] += 1
                elif value == "link":
                    self.SHARED_FILES[k]["MediaTypes"]["LINKS"] += 1
                elif value == "ptt":
                    self.SHARED_FILES[k]["MediaTypes"]["PTT"] += 1
                else:
                    self.SHARED_FILES[k]["MediaTypes"]["OTHER"] += 1
            self.SHARED_FILES[k]["NumberOfSharedItems"] = num_shared_items
        for k, _ in self.CONTACT_LIST.items():
            if k in self.SHARED_FILES:
                self.CONTACT_LIST[k]["MediaInCommon"] = self.SHARED_FILES[k][
                    "MediaTypes"
                ]
            if k in self.GALLERY_STATE:
                self.CONTACT_LIST[k]["GalleryContentDetails"] = self.GALLERY_STATE[k]
        msgs_sent = msgs_rcvd = total_sent = total_rcvd = 0
        for uid, msgs in self.MESSAGES.items():
            for msg, content in msgs.items():
                if "DIRECTION" in content["MESSAGE"]:
                    if content["MESSAGE"]["DIRECTION"] == "OUTGOING":
                        msgs_sent += 1
                    elif content["MESSAGE"]["DIRECTION"] == "INCOMING":
                        msgs_rcvd += 1
            if uid not in self.CONTACT_LIST:
                self.CONTACT_LIST[uid] = {
                    "MESSAGE_FROM_NON_CONTACT": uid,
                    "UID": uid,
                    "AIMID": uid,
                }
            self.CONTACT_LIST[uid]["MessagesSent"] = msgs_sent
            self.CONTACT_LIST[uid]["MessagesReceived"] = msgs_rcvd
            self.CONTACT_LIST[uid]["MessagesTotal"] = msgs_sent + msgs_rcvd
            total_sent += msgs_sent
            total_rcvd += msgs_rcvd
            msgs_sent = msgs_rcvd = 0
        self.INFO_CACHE["TOTAL_SENT"] = total_sent
        self.INFO_CACHE["TOTAL_RCVD"] = total_rcvd
        self.INFO_CACHE["TOTAL_ALL"] = total_sent + total_rcvd
        for uid, details in self.DIALOG_STATES.items():
            self.CONTACT_LIST[uid]["ConversationState"] = details
        for uid, content in self.SHARED_FILES.items():
            if uid in self.MESSAGES:
                msgs_data = self.MESSAGES[uid]
                for msg_id, _ in content.items():
                    if msg_id in msgs_data:
                        self.MESSAGES[uid][msg_id]["SharedContentDetails"] = content[
                            msg_id
                        ]
        for uid, content in self.MESSAGES.items():
            for msg_id, msg in content.items():
                if (
                    "SharedContentDetails" not in msg
                    and "TEXT" in msg["MESSAGE"]
                    and msg["MESSAGE"]["TEXT"].startswith("hxxps://files.icq.net")
                ):
                    uri_metadata = {}
                    uri = msg["MESSAGE"]["TEXT"].split("/get/")[1]
                    ftype, ftime, fsize = parse_file_id(uri)
                    uri_metadata["URI_DECODED_CONTENT_TYPE"] = ftype
                    uri_metadata["URI_DECODED_CONTENT_TIMESTAMP"] = ftime
                    uri_metadata["URI_DECODED_CONTENT_SIZE"] = fsize
                    self.MESSAGES[uid][msg_id]["SharedContentDetails"] = {
                        "URI_DECODED_METADATA": uri_metadata
                    }
                if (
                    "SharedContentDetails" not in msg
                    and "QUOTE_TEXT" in msg["MESSAGE"]
                    and msg["MESSAGE"]["QUOTE_TEXT"].startswith("hxxps://files.icq.net")
                ):
                    uri_metadata = {}
                    uri = msg["MESSAGE"]["QUOTE_TEXT"].split("/get/")[1]
                    ftype, ftime, fsize = parse_file_id(uri)
                    uri_metadata["URI_DECODED_CONTENT_TYPE"] = ftype
                    uri_metadata["URI_DECODED_CONTENT_TIMESTAMP"] = ftime
                    uri_metadata["URI_DECODED_CONTENT_SIZE"] = fsize
                    self.MESSAGES[uid][msg_id]["SharedContentDetails"] = {
                        "URI_DECODED_METADATA": uri_metadata
                    }


def parse_file_id(uri):
    ftype = timestamp = meta = None
    if len(uri) < 30:
        return [ftype, timestamp, meta]
    file_content = FileSharingUriParser(uri)
    ftype = file_content.extract_content_type()
    timestamp = file_content.decode_timestamp()
    if ftype.is_ptt():
        meta = file_content.decode_duration()
    elif ftype.is_image() or ftype.is_video():
        w, h = file_content.decode_size()
        meta = f"{w} * {h}"
    return [ftype.type_, timestamp, meta]


def sanitize(data):
    if data:
        data = data.replace("http", "hxxp")
        data = data.replace("ftp://", "fxx://")
    return data


def read_time(parser, chunk, blk, offset, update_ts=False):
    sig = chunk[0]
    length = chunk[1]
    lkup = parser.STRUCT_DICT[length]
    offset += 8
    unix_ts = struct.unpack_from(lkup, blk[offset : offset + length])[0]
    if unix_ts in {4294967295, 0}:
        ts = None
    else:
        ts = dt.fromtimestamp(unix_ts, timezone.utc).strftime(__fmt__)
    offset += length
    if update_ts:
        parser.TS = unix_ts
    if sig == 3:
        parser.RAW_TIME = unix_ts
    return ts, offset


def read_message_id(parser, chunk, blk, offset):
    length = chunk[1]
    offset += 8
    msg_id = struct.unpack_from("<Q", blk[offset : offset + length])[0]
    offset += length
    if msg_id == 18446744073709551615:
        return None, offset
    return msg_id, offset


def read_text(parser, chunk, blk, offset):
    sig = chunk[0]
    length = chunk[1]
    offset += 8
    text = blk[offset : offset + length].tobytes().decode("utf-8")
    if sig == 5:
        parser.msg = sanitize(text)
    offset += length
    return sanitize(text), offset


def read_bool(_, chunk, blk, offset):
    length = chunk[1]
    offset += 8
    bool_val = bool(struct.unpack_from("?", blk[offset : offset + length])[0])
    offset += length
    return bool_val, offset


def read_value(parser, chunk, blk, offset):
    sig = chunk[0]
    length = chunk[1]
    offset += 8
    if length == 0:
        return None, offset
    if sig == 69:
        lkup = ">H"
    else:
        lkup = parser.STRUCT_DICT[length]
    value = struct.unpack_from(lkup, blk[offset : offset + length])[0]
    offset += length
    return value, offset


def read_lookup_value(parser, chunk, blk, offset):
    sig = chunk[0]
    value, offset = read_value(parser, chunk, blk, offset)
    if sig == 23:
        value = parser.CHAT_EVENT[value]
    if sig == 27:
        value = parser.VOIP_EVENT[value]
    if sig == 31:
        value = parser.VOIP_DIRECTION[value]
    if sig == 69:
        value = bool(value)
    return value, offset


def read_size(parser, chunk, blk, offset):
    ## Size of the block to follow
    offset += 8
    return None, offset


def read_unknown(parser, chunk, blk, offset):
    # Once value relevance is defined, its parsing will be moved to another function
    # This will allow for effective processing of the data until this is determined.
    value_size = chunk[1]
    offset += 8
    if value_size == 0:
        pass
    else:
        lkup = parser.STRUCT_DICT[value_size]
        _ = struct.unpack_from(lkup, blk[offset : offset + value_size])[0]
    offset += value_size
    return None, offset


def read_chat_members(_, chunk, blk, offset):
    length = chunk[1]
    offset += 8
    members = {}
    blk_length = offset + length
    while offset < blk_length:
        mnum, name_length = struct.unpack_from("<II", blk[offset : offset + 8])
        offset += 8
        mname = blk[offset : offset + name_length].tobytes().decode("utf-8")
        members[mnum] = mname
        offset += name_length
    return members, offset


def read_message_flags(parser, chunk, blk, offset):
    # im-desktop/core/archive/message_flags.h
    length = chunk[1]
    offset += 8
    lkup = parser.STRUCT_DICT[length]
    value = struct.unpack_from(lkup, blk[offset : offset + length])[0]
    flags = {
        "UNUSED": bool(value & (1 << 0)),  # unused0_
        "UNREAD": bool(value & (1 << 1)),  # unread_
        "OUTGOING": bool(value & (1 << 2)),  # outgoing_
        "INVISIBLE": bool(value & (1 << 3)),  # invisible_
        "PATCH": bool(value & (1 << 4)),  # patch_
        "DELETED": bool(value & (1 << 5)),  # deleted_
        "MODIFIED": bool(value & (1 << 6)),  # modified_
        "UPDATED": bool(value & (1 << 7)),  # updated_
        "CLEAR": bool(value & (1 << 8)),  # clear_
        "RESTORED PATCH": bool(value & (1 << 9)),  # restored_patch_
    }
    _ = flags.pop("UNUSED")
    _ = flags.pop("PATCH")
    _ = flags.pop("RESTORED PATCH")
    offset += length
    return flags, offset


def read_format_flags(parser, chunk, blk, offset):
    # common.shared/message_processing/text_formatting.h
    length = chunk[1]
    offset += 8
    lkup = parser.STRUCT_DICT[length]
    value = struct.unpack_from(lkup, blk[offset : offset + length])[0]
    flags = {
        1 << 0: "bold",
        1 << 1: "italic",
        1 << 2: "underline",
        1 << 3: "strikethrough",
        1 << 4: "monospace",
        1 << 5: "link",
        1 << 6: "mention",
        1 << 7: "quote",
        1 << 8: "pre",
        1 << 9: "ordered_list",
        1 << 10: "unordered_list",
    }
    flags_set = [name for bit, name in flags.items() if value & bit]
    flags_set = "|".join(flags_set)
    offset += length
    return flags_set, offset


handlers = {
    # Handler codes sourced from icqdesktop.deprecated/core/archive/history_message.cpp
    # Types from there and corelib/enumerations.h
    0: (read_size, "CALL_LOG_CACHE_BLOCK_SIZE", None),
    1: (read_message_id, "MESSAGE_ID", "MESSAGE"),  # last 4 bytes is also time
    2: (read_message_flags, "FLAGS", "MESSAGE"), # Flags, incl direction, see read_message_flags
    3: (read_time, "TIME", "MESSAGE"),
    4: (read_text, "WID", "MESSAGE"),  # WIM ID
    5: (read_text, "TEXT", None),
    6: (read_size, "CHAT_BLOCK_SIZE", None),
    7: (read_size, "STICKER_BLOCK_SIZE", None),
    8: (read_size, "MULT", None),
    9: (read_size, "VOIP_BLOCK_SIZE", None),
    10: (read_text, "STICKER_ID", "MESSAGE"),
    11: (read_text, "CHAT_SENDER", "MESSAGE"),
    12: (read_text, "CHAT_NAME", "MESSAGE"),
    13: (read_message_id, "PREVIOUS_MESSAGE_ID_WITH_", "MESSAGE"),
    14: (read_text, "INTERNAL_ID", "MESSAGE"),
    15: (read_text, "CHAT_FRIENDLY_NAME", "MESSAGE"),
    16: (read_size, "FILE_SHARING_BLOCK_SIZE", None),
    17: (read_size, "FILE_SHARING_FLAGS", None),
    # 17 is file sharing outgoing but is no longer used
    18: (read_text, "FILE_SHARING_URI", "MESSAGE"),
    19: (read_text, "FILE_SHARING_LOCAL_PATH", "MESSAGE"),
    # 20 is file sharing upload ID but is no longer used
    20: (read_unknown, "FILE_SHARING_UPLOAD_ID", None),
    21: (read_text, "SENDER_FRIENDLY_NAME", "MESSAGE"),
    22: (read_size, "CHAT_EVENT_BLOCK_SIZE", None),
    23: (read_lookup_value, "CHAT_EVENT_TYPE", "MESSAGE"),
    24: (read_text, "CHAT_EVENT_SENDER_FRIENDLY_NAME", "MESSAGE"),
    25: (read_chat_members, "CHAT_EVENT_MCHAT_MEMBERS", "MESSAGE"),
    26: (read_text, "CHAT_EVENT_NEW_CHAT_NAME", "MESSAGE"),
    27: (read_lookup_value, "VOIP_EVENT_TYPE", "VOIP"),  # (missed, ended, accept, declined)
    28: (read_text, "VOIP_SENDER_FRIENDLY_NAME", "VOIP"),
    29: (read_text, "VOIP_SENDER_AIMID", "VOIP"),
    30: (read_value, "VOIP_DURATION", "VOIP"),
    31: (read_lookup_value, "VOIP_IS_INCOMING", "VOIP"),
    32: (read_text, "CHAT_EVENT_GENERIC TEXT", "MESSAGE"),
    33: (read_text, "CHAT_EVENT_NEW_CHAT_DESCRIPTION", "MESSAGE"),
    34: (read_text, "QUOTE_TEXT", "MESSAGE"),
    35: (read_text, "QUOTE_SENDER_SN", "MESSAGE"),
    36: (read_message_id, "QUOTE_MESSAGE_ID", "MESSAGE"),
    37: (read_time, "QUOTE_TIME", "MESSAGE"),
    38: (read_text, "QUOTE_CHAT_ID", "MESSAGE"),
    39: (read_size, "QUOTE", None),  # QUOTE (has a quote?)
    40: (read_text, "QUOTE_SENDER_FRIENDLY_NAME", "MESSAGE"),
    41: (read_bool, "QUOTE_IS_FORWARDED", "MESSAGE"),
    42: (read_text, "CHAT_EVENT_NEW_CHAT_RULES", "MESSAGE"),
    43: (read_text, "CHAT_EVENT_SENDER_AIMID", "MESSAGE"),
    44: (read_value, "QUOTE_SET_ID", None),
    45: (read_value, "QUOTE_STICKER_ID", None),
    46: (read_text, "QUOTE_CHAT_STAMP", "MESSAGE"),
    47: (read_text, "QUOTE_CHAT_NAME", "MESSAGE"),
    48: (read_size, "MENTION_BLOCK_SIZE", None),
    49: (read_text, "MENTIONER", "MESSAGE"),
    50: (read_text, "MENTIONER_FRIENDLY_NAME", "MESSAGE"),
    51: (read_chat_members, "CHAT_EVENT_MCHAT_MEMBERS_AIMIDS", "MESSAGE"),
    52: (read_text, "UPDATE_PATCH_VERSION", "MESSAGE"),
    53: (read_size, "SNIPPED_BLOCK_SIZE", None),
    54: (read_text, "SNIPPET_URL", "MESSAGE"),
    55: (read_text, "SNIPPET_CONTENT_TYPE", "MESSAGE"),
    56: (read_text, "SNIPPET_PREVIEW_URL", "MESSAGE"),
    57: (read_value, "SNIPPET_PREVIEW_WIDTH", "MESSAGE"),
    58: (read_value, "SNIPPET_PREVIEW_HEIGHT", "MESSAGE"),
    59: (read_text, "SNIPPET_PREVIEW_TITLE", "MESSAGE"),
    60: (read_text, "SNIPPET_DESCRIPTION", "MESSAGE"),
    61: (read_text, "VOIP_CONFERENCE_MEMBERS", "VOIP"),
    62: (read_bool, "VOIP_IS_VIDEO", "VOIP"),
    63: (read_size, "IS_CAPTCHA_PRESENT", None),  # Is Captcha Present in Chat Event
    64: (read_text, "DESCRIPTION", "MESSAGE"),
    65: (read_text, "URL", "MESSAGE"),
    66: (read_text, "QUOTE_URL", "MESSAGE"),
    67: (read_text, "QUOTE_DESCRIPTION", "MESSAGE"),
    68: (read_value, "OFFLINE_VERSION", None),
    69: (read_lookup_value, "IS_OFFICIAL", "MESSAGE"),
    70: (read_size, "SHARED_CONTACT", None),  # (bool?) - confirm
    71: (read_text, "SHARED_CONTACT_NAME", "MESSAGE"),
    72: (read_text, "SHARED_CONTACT_PHONE_NUMBER", "MESSAGE"),
    73: (read_text, "SHARED_CONTACT_SN", "MESSAGE"),
    74: (read_text, "FILE_SHARING_BASE_CONTENT_TYPE", "MESSAGE"),
    75: (read_value, "FILE_SHARING_DURATION", "MESSAGE"),
    76: (read_size, "GEO_DATA_BLOCK_SIZE", None),  # Geo Block Size"
    77: (read_text, "GEOGRAPHIC_NAME", "MESSAGE"),
    78: (read_text, "LATITUDE", "MESSAGE"),
    79: (read_text, "LONGITUDE", "MESSAGE"),
    80: (read_bool, "CHAT_IS_CHANNEL", "MESSAGE"),
    81: (read_size, "POLL_BLK_SIZE", None),
    82: (read_value, "POLL_ID", "MESSAGE"),
    83: (read_text, "POLL_ANSWER", "MESSAGE"),
    84: (read_value, "POLL_TYPE", "MESSAGE"),
    85: (read_text, "CHAT_EVENT_NEW_CHAT_STAMP", "MESSAGE"),
    86: (read_value, "JSON_BLOCK_SIZE", None),
    87: (read_text, "SENDER_AIMID", "MESSAGE"),
    88: (read_unknown, "BUTTONS", None),
    89: (read_bool, "HIDE_EDIT", None),
    90: (read_text, "CHAT_REQUESTED_BY", "MESSAGE"),
    91: (read_text, "CHAT_REQUESTER_FRIENDLY_NAME", "MESSAGE"),
    92: (read_text, "VOIP_CALL_AIMID", "VOIP"),
    93: (read_text, "VOIP_SID", "VOIP"),
    94: (read_size, "REACTIONS_BLOCK", None),
    95: (read_bool, "REACTIONS_EXISTS", "MESSAGE"),  # confirm format
    96: (read_text, "CHAT_EVENT_SENDER_STATUS", "MESSAGE"),
    97: (read_text, "CHAT_EVENT_OWNER_STATUS", "MESSAGE"),
    98: (read_text, "CHAT_EVENT_SENDER_STATUS_DESCRIPTION", "MESSAGE"),
    99: (read_text, "CHAT_EVENT_OWNER_STATUS_DESCRIPTION", "MESSAGE"),
    100: (read_size, "FORMAT_BLOCK_SIZE", None),  # Format
    101: (read_unknown, "FORMAT_OFFSET", None),
    102: (read_unknown, "FORMAT_LENGTH", None),
    103: (read_unknown, "FORMAT_DATA", None),
    104: (read_format_flags, "FORMAT_BOLD", None),  # not implemented at this time
    105: (read_format_flags, "FORMAT_ITALIC", None),
    106: (read_format_flags, "FORMAT_UNDERLINE", None),
    107: (read_format_flags, "FORMAT_STRIKETHROUGH", None),
    108: (read_format_flags, "FORMAT_INLINE_CODE", None),
    109: (read_format_flags, "FORMAT_URL", None),
    110: (read_format_flags, "FORMAT_MENTION", None),
    111: (read_format_flags, "FORMAT_QUOTE", None),
    112: (read_format_flags, "FORMAT_PRE", None),
    113: (read_format_flags, "FORMAT_ORDERED_LIST", None),
    114: (read_format_flags, "FORMAT_UNORDERED_LIST", None),
    115: (read_unknown, "DESCRIPTION_FORMAT", None),
    116: (read_size, "TASK_BLOCK_SIZE", None),
    117: (read_value, "TASK_ID", "MESSAGE"),
    118: (read_text, "TASK_TITLE", "MESSAGE"),
    119: (read_text, "TASK_ASSIGNEE", "MESSAGE"),
    120: (read_time, "TASK_END_TIME", "MESSAGE"),  # confirm format
    121: (read_value, "THREAD_ID", "MESSAGE"),
    122: (read_text, "TASK_STATUS", "MESSAGE"),
    123: (read_text, "CHAT_EVENT_TASK_EDITOR"),
    124: (read_unknown, "FORMAT_START_INDEX", None),
    125: (read_bool, "CHAT_EVENT_THREADS_ENABLED", "MESSAGE"),
}


my_info_handlers = {
    # im-desktop/core/connections/wim/my_info.h
    1: (read_text, "AIMID"),
    2: (read_text, "DISPLAY_ID"),
    3: (read_text, "FRIENDLY_NAME"),
    4: (read_text, "STATE"),
    5: (read_text, "USER_TYPE"),
    6: (read_text, "ATTACHED_PHONE_NUMBER"),
    7: (read_value, "GLOBAL_FLAGS"),
    8: (read_bool, "AUTO_CREATED"),
    9: (read_bool, "HAS_MAIL"),
    10: (read_bool, "READ_USER_AGREEMENT"),
    11: (read_bool, "ACCOUNT_IS_OFFICIAL"),
    12: (read_text, "NICKNAME"),
}

shared_files_handlers = {
    # core/archive/gallery_cache.cpp
    1: (read_size, "SHARED_CONTENT_BLOCK_SIZE", None),
    2: (read_message_id, "SHARED_CONTENT_MSG_ID", "FILE"),
    3: (read_value, "SHARED_SEQUENCE_NO", None),
    4: (read_message_id, "SHARED_CONTENT_NEXT_MSG_ID", "FILE"),
    5: (read_value, "SHARED_NEXT_SEQUENCE_NO", None),
    6: (read_text, "SHARED_CONTENT", "FILE"),
    7: (read_text, "SHARED_CONTENT_TYPE", "FILE"),
    8: (read_text, "SHARED_CONTENT_SENDER", "FILE"),
    9: (read_message_flags, "SHARED_MESSAGE_FLAGS", "FILE"),
    10: (read_time, "SHARED_CONTENT_TIME", "FILE"),
    11: (read_text, "SHARED_CONTENT_CAPTION", "FILE"),
}

draft_files_handlers = {
    # core/archive/draft_storage.h
    1: (read_value, "DRAFT_STATE", "DRAFT"),
    2: (read_time, "DRAFT_TIME", "DRAFT"),
    3: (read_size, "DRAFT_MESSAGE_BLOCK_SIZE", "DRAFT"),
    4: (read_time, "DRAFT_LOCAL_TIME", "DRAFT"),
    5: (read_text, "DRAFT_FRIENDLY_NAME", "DRAFT"),
    68: (read_value, "OFFLINE_VERSION", None),
    89: (read_bool, "HIDE_EDIT", None),
}

state_handlers = {
    # core/archive/gallery_cache.cpp ##
    1: (read_text, "PATCH_VERSION", None),
    2: (read_message_id, "LAST_ENTRY", "STATE"),
    3: (read_value, "LAST_ENTRY_SEQUENCE_NO", None),
    4: (read_message_id, "FIRST_ENTRY", "STATE"),
    5: (read_value, "FIRST_ENTRY_SEQUENCE_NO", None),
    6: (read_value, "IMAGE_COUNT", "STATE"),
    7: (read_value, "VIDEO_COUNT", "STATE"),
    8: (read_value, "FILE_COUNT", "STATE"),
    9: (read_value, "LINK_COUNT", "STATE"),
    10: (read_value, "PTT_COUNT", "STATE"),
    11: (read_value, "AUDIO_COUNT", "STATE"),
    12: (read_bool, "PATCH_VERSION_CHANGED", None),
}

dialog_state_handlers = {
    # im-desktop/core/archive/dlg_state.cpp
    1: (read_value, "UNREAD_COUNT", "DIALOG_STATE"),
    2: (read_message_id, "LAST_MESSAGE_ID", "DIALOG_STATE"),
    3: (read_message_id, "YOURS_LAST_READ", "DIALOG_STATE"),
    4: (read_message_id, "THEIRS_LAST_READ", "DIALOG_STATE"),
    5: (read_message_id, "THEIRS_LAST_DELIVERED", "DIALOG_STATE"),
    7: (read_size, "LAST_MESSAGE_CONTENT_SIZE", "DIALOG_STATE"),
    8: (read_bool, "VISIBLE", "DIALOG_STATE"),
    9: (read_unknown, "LAST_MESSAGE_FRIENDLY_UNUSED", None),  # Unused
    10: (read_text, "PATCH_VERSION", None),
    11: (read_message_id, "DEL_UP_TO", None),
    12: (read_text, "FRIENDLY_NAME", "DIALOG_STATE"),
    13: (read_bool, "OFFICIAL", "DIALOG_STATE"),
    14: (read_bool, "FAKE", "DIALOG_STATE"),
    15: (read_message_id, "HIDDEN_MESSAGE_ID", "DIALOG_STATE"),
    16: (read_value, "UNREAD_MENTIONS_COUNT", "DIALOG_STATE"),
    17: (read_unknown, "PINNED_MESSAGE", None),  # No sample yet
    18: (read_bool, "ATTENTION", None),  # No sample yet
    19: (read_bool, "SUSPICIOUS", None),  # No sample yet
    20: (read_unknown, "HEADS", None),  # No sample yet
    21: (read_text, "HEAD_AIMID", "DIALOG_STATE"),
    22: (read_size, "HEAD_FRIENDLY_BLOCK_SIZE", None),
    23: (read_message_id, "LAST_READ_MENTION", "DIALOG_STATE"),
    24: (read_bool, "STRANGER", "DIALOG_STATE"),  # No sample yet
    25: (read_text, "INFO_VERSION", None),  # Validate
    26: (read_value, "NO_RECENTS_UPDATE", None),  # No sample yet
    27: (read_text, "MEMBERS_VERSION", None),  # No sample yet.
}


class iOSParser:
    def __init__(self, start_path):
        folder_path = Path(start_path)
        self.folder_path = folder_path
        self.OWNER = {}
        self.AGENT = []
        self.AGENT_DB = ""
        self.AVATAR_PATHS = []
        self.CL = []
        self.CL_DB = ""
        self.DB_FILES = []
        self.FILES = []
        self.FILES_DB = ""
        self.FILE_CACHES = []
        self.FILE_DATA = {}
        self.ICQ_PLISTS = []
        self.SHARED = []
        self.SHARED_DB = ""
        self.TMP_FOLDER = []
        self.CONTACTS = {}
        self.MESSAGES = {}
        self.UID = ""
        self.BLANK_FILE = {
            "FILE_ID": None,
            "FILE_NAME": None,
            "FILE_CAPTION": None,
            "FILE_CONTENT_URL": None,
            "FILE_DURATION": None,
            "FILE_GALLERY_URL": None,
            "FILE_MESSAGE_ID": None,
            "FILE_NAME_EXISTS_IN_TMP": None,
            "FILE_ORIGINAL_CONTENT_NAME": None,
            "FILE_ORIGINAL_CONTENT_NAME_EXISTS": None,
            "FILE_PREVIEW_URL": None,
            "FILE_SENDER": None,
            "FILE_SIZE": None,
            "FILE_STORAGE_CONTENT_FILENAME": None,
            "FILE_STORAGE_CONTENT_FILENAME_EXISTS": None,
            "FILE_STORAGE_PREVIEW_FILENAME": None,
            "FILE_STORAGE_PREVIEW_FILENAME_EXISTS": None,
            "FILE_THUMBNAIL_DIMENSIONS": None,
            "FILE_TIME": None,
            "FILE_TYPE": None,
            "FILE_UPLOAD_REQUEST_ID": None,
            "FILE_UPLOAD_SOURCE_PATH": None,
            "FILE_UPLOAD_URL": None,
            "FILE_UPLOAD_USER_INITIATED": None,
            "FILE_URL": None,
        }
        self.BLANK_CONTACT = {
            "AbContactName": None,
            "About": None,
            "AbPhoneNumber": None,
            "AddressBookID": None,
            "AddressBookPhoneNumber": None,
            "AvatarOriginal": None,
            "AvatarOther": None,
            "AvatarPreview": None,
            "ChatDescription": None,
            "ChatInviter": None,
            "ChatIsPublic": None,
            "ChatParticipantCount": None,
            "ChatRules": None,
            "CommonChats": None,
            "ContactRowId": None,
            "ContactType": None,
            "DisplayName": None,
            "FirstName": None,
            "FirstName_Anketa": None,
            "FriendlyName": None,
            "GroupId": None,
            "Ignored": None,
            "IsBlocked": None,
            "IsInAddressBook": None,
            "IsMuted": None,
            "LargeIconId": None,
            "LastMessageDeliveredFromThem": None,
            "LastMessageId": None,
            "LastMessageOfYoursLastRead": None,
            "LastMessagePK": None,
            "LastMessageReadLocally": None,
            "LastMessageText": None,
            "LastName": None,
            "LastName_Anketa": None,
            "MessagesReceived": 0,
            "MessagesSent": 0,
            "MessagesTotal": 0,
            "NormalizedPhoneNumber": None,
            "SharedDisplayName": None,
            "SharedLastMessageTime": None,
            "SharedPhoneNumber": None,
            "StatusCustomText": None,
            "StatusEndTime": None,
            "StatusLastSeenTime": None,
            "StatusMedia": None,
            "StatusStartTime": None,
            "UID": None,
            "UpdateLocalTime": None,
            "UserType": None,
        }
        # identified through log files and data comparison within iOS ICQ databases
        self.USER_TYPE = {2: "icq", 3: "interop", 4: "sms", 5: "chat"}
        # core/connections/wim/lastseen.h
        self.USER_STATE = {0: "active", 1: "absent", 2: "blocked", 3: "bot"}
        # im-desktop/gui/main_window/MainPage.h
        self.VOIP_CALL_TYPE = {0: "audio", 1: "video"}
        # im-desktop/core/Voip/VoipManagerDefines.h - TerminateReason
        # im-desktop/core/Voip/libvoip/include/voip/voip3.h
        self.VOIP_END_REASON = {
            0: "hangup (after accepted)",
            1: "reject",
            2: "busy",
            3: "handled by another instance (call handled by another device of logged-in user)",
            4: "unauthorized due to security errors",
            5: "allocate failed",
            6: "answer timeout (party did not accept or reject call)",
            7: "connect timeout (connection could not be established or was lost)",
            8: "not found",
            9: "blocked by caller (is stranger)",
            10: "blocked by callee privacy",
            11: "call must be authorized by captcha",
            12: "bad uri",
            13: "not available now",
            14: "participants limit exceeded",
            15: "duration limit exceeded",
            16: "internal error",
        }
        self.MSG_DIRECTION = {0: "INCOMING", 1: "OUTGOING"}
        self.CALL_DIRECTION = {0: "OUTGOING", 1: "INCOMING"}
        self.PDFS = []
        for file in folder_path.rglob("*"):
            if file.match("*.sqlite"):
                self.DB_FILES.append(str(file))
                if file.match("Agent.sqlite"):
                    self.AGENT_DB = str(file)
                elif file.match("cl.sqlite"):
                    self.CL_DB = str(file)
                elif file.match("files.sqlite"):
                    self.FILES_DB = str(file)
                elif file.match("Shared.sqlite"):
                    self.SHARED_DB = str(file)
            elif file.match("*fileXferCache*"):
                self.FILE_CACHES.append(str(file))
            elif file.match("tmp"):
                self.TMP_FOLDER.append(str(file))
            elif file.match("avatars"):
                self.AVATAR_PATHS.append(str(file))
            elif file.match("group.com.icq.icqfree.plist"):
                self.ICQ_PLISTS.append(str(file))
            elif file.match("com.icq.icqfree.plist"):
                self.ICQ_PLISTS.append(str(file))
        self.get_uid()

    def get_contacts(self):
        cIdx, contact_data = split_table(self.CL["contact"])
        abIdx, ab_data = split_table(self.CL["ab_person"])
        ankIdx, anketa = split_table(self.CL["anketa"])
        chatIdx, chat_data = split_table(self.CL["chat_info"])
        contactIdx, contact_grp_data = split_table(self.CL["contact_group"])
        contact_group_mapping = {}
        for each_group in contact_grp_data:
            contact_group_mapping[each_group[contactIdx["groupID"]]] = each_group[
                contactIdx["name"]
            ]
        if 0 not in contact_group_mapping:
            contact_group_mapping[0] = "No identified Group - Temporary"
        orderIdx, order_data = split_table(self.CL["contact_order"])
        statusIdx, status_data = split_table(self.CL["status"])
        sharedIdx, shared_data = split_table(self.SHARED["contact"])
        ZMRCONVERSATION_hdr, ZMRCONVERSATION = split_table(
            self.AGENT["ZMRCONVERSATION"]
        )
        ZMRMESSAGE_hdr, ZMRMESSAGE = split_table(self.AGENT["ZMRMESSAGE"])
        ZMRGALLERYSTATE_hdr, ZMRGALLERYSTATE = split_table(
            self.AGENT["ZMRGALLERYSTATE"]
        )
        ZMRCHATSEARCHQUERY_hdr, ZMRCHATSEARCHQUERY = split_table(
            self.AGENT["ZMRCHATSEARCHQUERY"]
        )
        if self.UID == "":
            self.UID = contact_data[0][cIdx["profilePID"]].split("|wim")[0]
        for this_contact in contact_data:
            uid = this_contact[cIdx["uid"]]
            rid = this_contact[cIdx["rowid"]]
            pid = this_contact[cIdx["pid"]]
            if uid in self.CONTACTS:
                uid = f"{uid}_2"
            self.CONTACTS[uid] = self.BLANK_CONTACT.copy()
            self.CONTACTS[uid]["UID"] = uid
            if "@chat.agent" in uid:
                self.CONTACTS[uid]["ContactType"] = "GROUP CHAT"
            else:
                self.CONTACTS[uid]["ContactType"] = "PRIVATE"
            self.CONTACTS[uid]["DisplayName"] = this_contact[cIdx["displayName"]]
            self.CONTACTS[uid]["IsBlocked"] = bool(this_contact[cIdx["blocked"]])
            self.CONTACTS[uid]["IsIgnored"] = bool(this_contact[cIdx["ignored"]])
            self.CONTACTS[uid]["IsMuted"] = bool(this_contact[cIdx["isMute"]])
            self.CONTACTS[uid]["UserType"] = self.USER_TYPE[
                this_contact[cIdx["userType"]]
            ]
            self.CONTACTS[uid]["NickName"] = this_contact[cIdx["nickname"]]
            self.CONTACTS[uid]["ContactRowId"] = rid
            self.CONTACTS[uid]["GroupId"] = contact_group_mapping[
                this_contact[cIdx["groupId"]]
            ]
            inAddrBook = bool(this_contact[cIdx["isFromAddressBook"]])
            self.CONTACTS[uid]["IsInAddressBook"] = inAddrBook
            if inAddrBook:
                for entry in ab_data:
                    if (
                        entry[abIdx["compositeName"]]
                        == this_contact[cIdx["displayName"]]
                    ):
                        self.CONTACTS[uid]["FirstName"] = entry[abIdx["firstName"]]
                        self.CONTACTS[uid]["LastName"] = entry[abIdx["lastName"]]
                        self.CONTACTS[uid]["AddressBookID"] = entry[
                            abIdx["abContactID"]
                        ].split(":ABPerson")[0]
                        self.CONTACTS[uid]["AddressBookPhoneNumber"] = entry[
                            abIdx["phones"]
                        ]
            for ank_data in anketa:
                if ank_data[ankIdx["contactID"]] == rid:
                    self.CONTACTS[uid]["AbContactName"] = ank_data[
                        ankIdx["abContactName"]
                    ]
                    self.CONTACTS[uid]["About"] = ank_data[ankIdx["about"]]
                    self.CONTACTS[uid]["CommonChats"] = ank_data[ankIdx["commonChats"]]
                    self.CONTACTS[uid]["FirstName_Anketa"] = ank_data[
                        ankIdx["firstName"]
                    ]
                    self.CONTACTS[uid]["LastName_Anketa"] = ank_data[ankIdx["lastName"]]
                    if int(ank_data[ankIdx["anketaUpdateLocalTime"]]) > 0:
                        ank_update_time = convert_unix_ts(
                            ank_data[ankIdx["anketaUpdateLocalTime"]]
                        )
                        self.CONTACTS[uid]["UpdateLocalTime"] = ank_update_time
                    else:
                        self.CONTACTS[uid]["UpdateLocalTime"] = ank_data[
                            ankIdx["anketaUpdateLocalTime"]
                        ]
                    self.CONTACTS[uid]["AbPhoneNumber"] = ank_data[
                        ankIdx["abPhoneNumber"]
                    ]
                    self.CONTACTS[uid]["NormalizedPhoneNumber"] = ank_data[
                        ankIdx["normalizedPhoneNumber"]
                    ]
                    self.CONTACTS[uid]["FriendlyName"] = ank_data[ankIdx["friendly"]]
                    self.CONTACTS[uid]["LargeIconId"] = ank_data[ankIdx["largeIconId"]]
                    break
            for chat in chat_data:
                if chat[chatIdx["contactID"]] == rid:
                    self.CONTACTS[uid]["ChatDescription"] = chat[
                        chatIdx["chatDescription"]
                    ]
                    self.CONTACTS[uid]["ChatParticipantCount"] = chat[
                        chatIdx["chatParticipantsCount"]
                    ]
                    self.CONTACTS[uid]["ChatIsPublic"] = bool(chat[chatIdx["isPublic"]])
                    self.CONTACTS[uid]["ChatRules"] = chat[chatIdx["rules"]]
                    self.CONTACTS[uid]["ChatInviter"] = chat[chatIdx["inviter"]]
                    self.CONTACTS[uid]["ChatStamp"] = chat[chatIdx["stamp"]]
            for order in order_data:
                if order[orderIdx["contactID"]] == rid:
                    self.CONTACTS[uid]["ContactOrderSubtitle"] = order[
                        orderIdx["subtitle"]
                    ]
            for status in status_data:
                if status[statusIdx["contactID"]] == rid:
                    if status[statusIdx["lastSeen"]] > 0:
                        status_last_seen = convert_unix_ts(
                            status[statusIdx["lastSeen"]]
                        )
                        self.CONTACTS[uid]["StatusLastSeenTime"] = status_last_seen
                    else:
                        self.CONTACTS[uid]["StatusLastSeenTime"] = status[
                            statusIdx["lastSeen"]
                        ]
                    if status[statusIdx["startTime"]] > 0:
                        status_start = convert_unix_ts(status[statusIdx["startTime"]])
                        self.CONTACTS[uid]["StatusStartTime"] = status_start
                    else:
                        self.CONTACTS[uid]["StatusStartTime"] = status[
                            statusIdx["startTime"]
                        ]
                    if status[statusIdx["endTime"]] > 0:
                        status_end = convert_unix_ts(status[statusIdx["endTime"]])
                        self.CONTACTS[uid]["StatusEndTime"] = status_end
                    else:
                        self.CONTACTS[uid]["StatusEndTime"] = status[
                            statusIdx["endTime"]
                        ]
                    self.CONTACTS[uid]["StatusMedia"] = status[statusIdx["media"]]
                    self.CONTACTS[uid]["StatusCustomText"] = status[
                        statusIdx["customText"]
                    ]
                    self.CONTACTS[uid]["UserState"] = self.USER_STATE[
                        status[statusIdx["userState"]]
                    ]
            if self.AVATAR_PATHS:
                for path in self.AVATAR_PATHS:
                    avatar_preview = os.path.abspath(f"{path}{os.sep}{uid}_preview")
                    avatar_original = os.path.abspath(f"{path}{os.sep}{uid}_original")
                    avatar_other = os.path.abspath(
                        f'{path}{os.sep}{pid.replace("|","_")}'
                    )
                    if os.path.exists(avatar_preview):
                        self.CONTACTS[uid]["AvatarPreview"] = avatar_preview
                    if os.path.exists(avatar_original):
                        self.CONTACTS[uid]["AvatarOriginal"] = avatar_original
                    if os.path.exists(avatar_other):
                        self.CONTACTS[uid]["AvatarOther"] = avatar_other
            for contact in shared_data:
                if contact[sharedIdx["pid"]].split("|wim|")[1] == uid:
                    self.CONTACTS[uid]["SharedDisplayName"] = contact[
                        sharedIdx["display_name"]
                    ]
                    shared_phone = contact[sharedIdx["phone_number"]]
                    if shared_phone is None:
                        shared_phone = ""
                    self.CONTACTS[uid]["SharedPhoneNumber"] = shared_phone
                    if contact[sharedIdx["last_message_time"]] > 0:
                        last_msg_time = convert_unix_ts(
                            contact[sharedIdx["last_message_time"]]
                        )
                        self.CONTACTS[uid]["SharedLastMessageTime"] = last_msg_time
                    else:
                        self.CONTACTS[uid]["SharedLastMessageTime"] = contact[
                            sharedIdx["last_message_time"]
                        ]
            for convo in ZMRCONVERSATION:
                convo_party = convo[ZMRCONVERSATION_hdr["ZPID"]].split("|wim|")[1]
                if convo_party == uid:
                    self.CONTACTS[uid]["LastMessageId"] = convo[
                        ZMRCONVERSATION_hdr["ZLASTMESSAGEID"]
                    ]
                    self.CONTACTS[uid]["LastMessageReadLocally"] = convert_tiktok_ts(
                        convo[ZMRCONVERSATION_hdr["ZLOCALREAD"]]
                    )
                    self.CONTACTS[uid]["LastMessageDeliveredFromThem"] = (
                        convert_tiktok_ts(
                            convo[ZMRCONVERSATION_hdr["ZTHEIRSLASTDELIVERED"]]
                        )
                    )
                    self.CONTACTS[uid]["LastMessageReadFromThem"] = convert_tiktok_ts(
                        convo[ZMRCONVERSATION_hdr["ZTHEIRSLASTREAD"]]
                    )
                    self.CONTACTS[uid]["LastMessageOfYoursLastRead"] = (
                        convert_tiktok_ts(convo[ZMRCONVERSATION_hdr["ZYOURSLASTREAD"]])
                    )
                    self.CONTACTS[uid]["LastMessagePK"] = convo[
                        ZMRCONVERSATION_hdr["ZLASTMESSAGE"]
                    ]
                    for message in ZMRMESSAGE:
                        if (
                            message[ZMRMESSAGE_hdr["Z_PK"]]
                            == convo[ZMRCONVERSATION_hdr["ZLASTMESSAGE"]]
                        ):
                            self.CONTACTS[uid]["LastMessageText"] = message[
                                ZMRMESSAGE_hdr["ZTEXTSTRING"]
                            ]
            for contact in ZMRGALLERYSTATE:
                if contact[ZMRGALLERYSTATE_hdr["ZPID"]].split("|wim|")[1] == uid:
                    self.CONTACTS[uid]["MediaInCommon"] = {
                        "FILES": contact[ZMRGALLERYSTATE_hdr["ZCOUNTFILES"]],
                        "IMAGES": contact[ZMRGALLERYSTATE_hdr["ZCOUNTIMAGES"]],
                        "LINKS": contact[ZMRGALLERYSTATE_hdr["ZCOUNTLINKS"]],
                        "PTT_CONVERSATIONS": contact[ZMRGALLERYSTATE_hdr["ZCOUNTPTT"]],
                        "VIDEOS": contact[ZMRGALLERYSTATE_hdr["ZCOUNTVIDEO"]],
                    }
        for uid, values in self.CONTACTS.items():
            if (
                "ChatInviter" in values
                and values["ChatInviter"] is not None
                and values["ChatInviter"] != ""
            ):
                self.CONTACTS[uid]["ChatInviter"] = {
                    self.CONTACTS[uid]["ChatInviter"]: self.CONTACTS[
                        self.CONTACTS[uid]["ChatInviter"]
                    ]
                }
        self.OWNER = self.CONTACTS[self.UID]
        if ZMRCHATSEARCHQUERY:
            QUERIES = {}
            for query in ZMRCHATSEARCHQUERY:
                ZDATE = query[ZMRCHATSEARCHQUERY_hdr["ZDATE"]]
                QUERIES[ZDATE] = {}
                QUERIES[ZDATE]["DATE"] = convert_nsdate_ts(
                    query[ZMRCHATSEARCHQUERY_hdr["ZDATE"]]
                )
                QUERIES[ZDATE]["QUERY"] = query[ZMRCHATSEARCHQUERY_hdr["ZQUERY"]]
        self.OWNER["SearchQueries"] = QUERIES
        return self.CONTACTS

    def get_messages(self):
        ZMRMESSAGE_hdr, ZMRMESSAGE = split_table(self.AGENT["ZMRMESSAGE"])
        ## archive_idx = ZMRMESSAGE_hdr["ZARCHIVEID"]
        ## orderkey_idx = ZMRMESSAGE_hdr["ZORDERKEY"]
        ## TODO: Use History ID as Message ID
        ZMRCALLMESSAGE_hdr, ZMRCALLMESSAGE = split_table(self.AGENT["ZMRCALLMESSAGE"])
        ZMRGALLERYENTRY_hdr, ZMRGALLERYENTRY = split_table(
            self.AGENT["ZMRGALLERYENTRY"]
        )
        FILE_ENTRIES_hdr, file_data = split_table(self.FILES["file"])
        ZMRMESSAGEPART_hdr, ZMRMESSAGEPART = split_table(self.AGENT["ZMRMESSAGEPART"])
        ZMRMESSAGEPARTQUOTEINFO_hdr, ZMRMESSAGEPARTQUOTEINFO = split_table(
            self.AGENT["ZMRMESSAGEPARTQUOTEINFO"]
        )
        # ZMRREACTION_hdr, ZMRREACTION = split_table(self.AGENT["ZMRREACTION"])
        ZMRREACTIONITEM_hdr, ZMRREACTIONITEM = split_table(
            self.AGENT["ZMRREACTIONITEM"]
        )
        for this_message in ZMRMESSAGE:
            pid = this_message[ZMRMESSAGE_hdr["ZCONTACTPID"]]
            uid = pid.split("|wim|")[1]
            mid = this_message[ZMRMESSAGE_hdr["Z_PK"]]
            if uid not in self.MESSAGES:
                self.MESSAGES[uid] = {}
            self.MESSAGES[uid][mid] = {"MESSAGE": {}, "UID": uid}
            self.MESSAGES[uid][mid]["MESSAGE"]["TIME"] = convert_nsdate_ts(
                this_message[ZMRMESSAGE_hdr["ZTIME"]]
            )
            if this_message[ZMRMESSAGE_hdr["ZUPDATETIME"]] is not None:
                self.MESSAGES[uid][mid]["MESSAGE"]["UPDATETIME"] = convert_nsdate_ts(
                    this_message[ZMRMESSAGE_hdr["ZUPDATETIME"]]
                )
            else:
                self.MESSAGES[uid][mid]["MESSAGE"]["UPDATETIME"] = None
            self.MESSAGES[uid][mid]["MESSAGE"]["TIME_RAW"] = this_message[
                ZMRMESSAGE_hdr["ZTIME"]
            ]
            self.MESSAGES[uid][mid]["MESSAGE"]["WAS_READ"] = bool(
                this_message[ZMRMESSAGE_hdr["ZWASREAD"]]
            )
            self.MESSAGES[uid][mid]["MESSAGE"]["DIRECTION"] = self.MSG_DIRECTION[
                this_message[ZMRMESSAGE_hdr["ZOUTGOING"]]
            ]
            text_string = sanitize(this_message[ZMRMESSAGE_hdr["ZTEXTSTRING"]])
            if this_message[ZMRMESSAGE_hdr["ZSTATUSREPLY"]] is None:
                self.MESSAGES[uid][mid]["MESSAGE"]["TEXT"] = text_string
            else:
                self.MESSAGES[uid][mid]["MESSAGE"][
                    "TEXT"
                ] = f"AUTO-REPLY: {text_string}"
            attr = this_message[ZMRMESSAGE_hdr["ZTEXTATTRIBUTES"]]
            if isinstance(attr, bytes):
                plist_data = plistlib.loads(attr)
                self.MESSAGES[uid][mid]["MESSAGE"]["TEXT_ATTRIBUTES"] = str(plist_data)
            self.MESSAGES[uid][mid]["MESSAGE"]["HISTORY_ID"] = this_message[
                ZMRMESSAGE_hdr["ZHISTORYID"]
            ]
            self.MESSAGES[uid][mid]["MESSAGE"]["PARTICIPANT"] = this_message[
                ZMRMESSAGE_hdr["ZPARTICIPANTUID"]
            ]
            attr = this_message[ZMRMESSAGE_hdr["ZADDEDPARTICIPANTSDATA"]]
            if isinstance(attr, bytes):
                plist_data = plistlib.loads(attr)
                self.MESSAGES[uid][mid]["MESSAGE"]["ADDL_PARTICIPANT_DATA"] = str(
                    plist_data
                )
            self.MESSAGES[uid][mid]["MESSAGE"][
                "CONTACT"
            ] = f'{uid} - {self.CONTACTS[uid]["DisplayName"]}'
            # IF ZPARTICIPANTUID is not null, ZPARTICIPANTUID is person responding in chat 
            # (to @chat.agent). Otherwise, ZCONTACTPID is responding to UID owner.
            # This concats PARTICIPANT AND CONTACT for messages and uses 
            # "CONTACT_PARTICIPANT" for Messages            
            if self.MESSAGES[uid][mid]["MESSAGE"]["PARTICIPANT"]:
                self.MESSAGES[uid][mid]["MESSAGE"][
                    "CONTACT_PARTICIPANT"
                ] = f'[{uid}: {this_message[ZMRMESSAGE_hdr["ZPARTICIPANTUID"]]}]'
            else:
                self.MESSAGES[uid][mid]["MESSAGE"]["CONTACT_PARTICIPANT"] = f"[{uid}]"
            for msg_part in ZMRMESSAGEPART:
                parid = msg_part[ZMRMESSAGEPART_hdr["ZPARENT"]]
                if parid == mid:
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_ID"] = msg_part[
                        ZMRMESSAGEPART_hdr["Z_PK"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_COORD_LAT"] = msg_part[
                        ZMRMESSAGEPART_hdr["ZLATITUDE"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_COORD_LONG"] = msg_part[
                        ZMRMESSAGEPART_hdr["ZLONGITUDE"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_ALT_TEXT"] = msg_part[
                        ZMRMESSAGEPART_hdr["ZALTTEXT"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_CAPTION"] = msg_part[
                        ZMRMESSAGEPART_hdr["ZCAPTION"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_CAPTION_URL"] = sanitize(
                        msg_part[ZMRMESSAGEPART_hdr["ZCAPTIONURL"]]
                    )
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_CONTACTNAME"] = msg_part[
                        ZMRMESSAGEPART_hdr["ZCONTACTNAME"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_CONTACTPHONE"] = msg_part[
                        ZMRMESSAGEPART_hdr["ZCONTACTPHONE"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_CONTACTUID"] = msg_part[
                        ZMRMESSAGEPART_hdr["ZCONTACTUID"]
                    ]
                    self.MESSAGES[uid][mid]["MESSAGE"]["PART_TEXT"] = sanitize(
                        msg_part[ZMRMESSAGEPART_hdr["ZTEXTSTRING"]]
                    )
                    attr = msg_part[ZMRMESSAGEPART_hdr["ZTEXTATTRIBUTES"]]
                    if isinstance(attr, bytes):
                        plist_data = plistlib.loads(attr)
                        self.MESSAGES[uid][mid]["MESSAGE"]["PART_TEXT_ATTRIB"] = str(
                            plist_data
                        )
                    quote_info = msg_part[ZMRMESSAGEPART_hdr["ZQUOTEINFO"]]
                    if quote_info:
                        for qinfo in ZMRMESSAGEPARTQUOTEINFO:
                            if qinfo[ZMRMESSAGEPARTQUOTEINFO_hdr["Z_PK"]] == quote_info:
                                self.MESSAGES[uid][mid]["MESSAGE"]["PART_TEXT_TIME"] = (
                                    convert_nsdate_ts(
                                        qinfo[ZMRMESSAGEPARTQUOTEINFO_hdr["ZTIME"]]
                                    )
                                )
                                self.MESSAGES[uid][mid]["MESSAGE"]["PART_ROOT_CHAT"] = (
                                    qinfo[ZMRMESSAGEPARTQUOTEINFO_hdr["ZROOTCHAT"]]
                                )
                                self.MESSAGES[uid][mid]["MESSAGE"][
                                    "PART_ROOT_SENDER"
                                ] = qinfo[ZMRMESSAGEPARTQUOTEINFO_hdr["ZROOTSENDER"]]
                                self.MESSAGES[uid][mid]["MESSAGE"][
                                    "PART_IS_FORWARDED"
                                ] = bool(
                                    qinfo[ZMRMESSAGEPARTQUOTEINFO_hdr["ZISFORWARD"]]
                                )
            call_info = this_message[ZMRMESSAGE_hdr["ZCALLINFO"]]
            if call_info:
                self.MESSAGES[uid][mid]["CALL_INFO"] = {}
                for call in ZMRCALLMESSAGE:
                    if call[ZMRCALLMESSAGE_hdr["Z_PK"]] == call_info:
                        self.MESSAGES[uid][mid]["CALL_INFO"]["BUDDY_ID"] = call[
                            ZMRCALLMESSAGE_hdr["ZBUDDYUID"]
                        ]
                        self.MESSAGES[uid][mid]["CALL_INFO"]["BUDDY_NAME"] = call[
                            ZMRCALLMESSAGE_hdr["ZBUDDYNAME"]
                        ]
                        self.MESSAGES[uid][mid]["CALL_INFO"]["DIRECTION"] = (
                            self.CALL_DIRECTION[call[ZMRCALLMESSAGE_hdr["ZINCOMING"]]]
                        )
                        self.MESSAGES[uid][mid]["CALL_INFO"]["DURATION"] = call[
                            ZMRCALLMESSAGE_hdr["ZDURATION"]
                        ]
                        self.MESSAGES[uid][mid]["CALL_INFO"]["MISSED"] = bool(
                            call[ZMRCALLMESSAGE_hdr["ZMISSED"]]
                        )
                        self.MESSAGES[uid][mid]["CALL_INFO"]["DATE"] = (
                            convert_nsdate_ts(call[ZMRCALLMESSAGE_hdr["ZDATE"]])
                        )
                        self.MESSAGES[uid][mid]["CALL_INFO"]["CALL_ID"] = call[
                            ZMRCALLMESSAGE_hdr["ZCALLID"]
                        ]
                        self.MESSAGES[uid][mid]["CALL_INFO"]["GROUP_CALL_MEMBERS"] = (
                            call[ZMRCALLMESSAGE_hdr["ZGROUPCALLMEMBERS"]]
                        )
                        self.MESSAGES[uid][mid]["CALL_INFO"]["VOIP_ID"] = call[
                            ZMRCALLMESSAGE_hdr["ZVOIPID"]
                        ]
                        self.MESSAGES[uid][mid]["CALL_INFO"]["MESSAGE_PARENT_ID"] = (
                            call[ZMRCALLMESSAGE_hdr["ZPARENT"]]
                        )
                        self.MESSAGES[uid][mid]["CALL_INFO"]["CALL_TYPE"] = (
                            self.VOIP_CALL_TYPE[call[ZMRCALLMESSAGE_hdr["ZCALLTYPE"]]]
                        )
                        self.MESSAGES[uid][mid]["CALL_INFO"]["END_REASON"] = (
                            self.VOIP_END_REASON[call[ZMRCALLMESSAGE_hdr["ZENDREASON"]]]
                        )
            if this_message[ZMRMESSAGE_hdr["ZFILEID"]] != 0:
                fid = this_message[ZMRMESSAGE_hdr["ZFILEID"]]
                self.MESSAGES[uid][mid]["MESSAGE"]["FILE_ID"] = fid
                self.MESSAGES[uid][mid]["FILE"] = {}
                self.MESSAGES[uid][mid]["FILE"][fid] = self.BLANK_FILE.copy()
                self.MESSAGES[uid][mid]["FILE"][fid]["FILE_ID"] = fid
                for file_entry in ZMRGALLERYENTRY:
                    if (
                        file_entry[ZMRGALLERYENTRY_hdr["ZFILEID"]] == fid
                        and file_entry[ZMRGALLERYENTRY_hdr["ZPID"]].split("|wim|")[1]
                        == uid
                    ):
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_GALLERY_URL"] = (
                            sanitize(file_entry[ZMRGALLERYENTRY_hdr["ZURL"]])
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_CAPTION"] = (
                            file_entry[ZMRGALLERYENTRY_hdr["ZCAPTION"]]
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_SENDER"] = (
                            file_entry[ZMRGALLERYENTRY_hdr["ZSENDERUID"]]
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_TIME"] = (
                            convert_unix_ts(file_entry[ZMRGALLERYENTRY_hdr["ZTIME"]])
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_MESSAGE_ID"] = (
                            file_entry[ZMRGALLERYENTRY_hdr["ZMESSAGEID"]]
                        )
                for file_entry in file_data:
                    if file_entry[FILE_ENTRIES_hdr["file_id"]] == fid:
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_SIZE"] = file_entry[
                            FILE_ENTRIES_hdr["filesize"]
                        ]
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_NAME"] = sanitize(
                            file_entry[FILE_ENTRIES_hdr["filename"]]
                        )

                        if self.TMP_FOLDER:
                            for folder in self.TMP_FOLDER:
                                if len(self.TMP_FOLDER) > 1:
                                    label = f"FILE_NAME_EXISTS_IN_TMP_{self.TMP_FOLDER.index(folder)}"
                                else:
                                    label = "FILE_NAME_EXISTS_IN_TMP"
                                if file_entry[
                                    FILE_ENTRIES_hdr["filename"]
                                ] in os.listdir(folder):
                                    self.MESSAGES[uid][mid]["FILE"][fid][label] = (
                                        os.path.normpath(
                                            os.path.abspath(
                                                f'{folder}{os.sep}{file_entry[FILE_ENTRIES_hdr["filename"]]}'
                                            )
                                        )
                                    )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_CONTENT_URL"] = (
                            sanitize(file_entry[FILE_ENTRIES_hdr["content_url"]])
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_TYPE"] = file_entry[
                            FILE_ENTRIES_hdr["mimetype"]
                        ]
                        if (
                            file_entry[FILE_ENTRIES_hdr["original_content_filename"]]
                            != ""
                            and file_entry[
                                FILE_ENTRIES_hdr["original_content_filename"]
                            ]
                            is not None
                        ):
                            self.MESSAGES[uid][mid]["FILE"][fid][
                                "FILE_ORIGINAL_CONTENT_NAME"
                            ] = file_entry[
                                FILE_ENTRIES_hdr["original_content_filename"]
                            ]
                        if (
                            file_entry[FILE_ENTRIES_hdr["storage_content_filename"]]
                            != ""
                            and file_entry[FILE_ENTRIES_hdr["storage_content_filename"]]
                            is not None
                        ):
                            self.MESSAGES[uid][mid]["FILE"][fid][
                                "FILE_STORAGE_CONTENT_FILENAME"
                            ] = file_entry[FILE_ENTRIES_hdr["storage_content_filename"]]
                        self.MESSAGES[uid][mid]["FILE"][fid][
                            "FILE_STORAGE_PREVIEW_FILENAME"
                        ] = file_entry[FILE_ENTRIES_hdr["storage_preview_filename"]]
                        if self.FILE_CACHES:
                            for cache in self.FILE_CACHES:
                                fp = os.path.abspath(
                                    f'{cache}{os.sep}{file_entry[FILE_ENTRIES_hdr["original_content_filename"]]}'
                                )
                                if os.path.exists(fp) and os.path.isfile(fp):
                                    self.MESSAGES[uid][mid]["FILE"][fid][
                                        "FILE_ORIGINAL_CONTENT_NAME_EXISTS"
                                    ] = fp
                                fp = os.path.abspath(
                                    f'{cache}{os.sep}{file_entry[FILE_ENTRIES_hdr["storage_content_filename"]]}'
                                )
                                if os.path.exists(fp) and os.path.isfile(fp):
                                    self.MESSAGES[uid][mid]["FILE"][fid][
                                        "FILE_STORAGE_CONTENT_FILENAME_EXISTS"
                                    ] = fp
                                fp = os.path.abspath(
                                    f'{cache}{os.sep}{file_entry[FILE_ENTRIES_hdr["storage_preview_filename"]]}'
                                )
                                if os.path.exists(fp) and os.path.isfile(fp):
                                    self.MESSAGES[uid][mid]["FILE"][fid][
                                        "FILE_STORAGE_PREVIEW_FILENAME_EXISTS"
                                    ] = fp
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_DURATION"] = (
                            file_entry[FILE_ENTRIES_hdr["duration"]]
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_PREVIEW_URL"] = (
                            sanitize(file_entry[FILE_ENTRIES_hdr["preview_url"]])
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid][
                            "FILE_UPLOAD_REQUEST_ID"
                        ] = file_entry[FILE_ENTRIES_hdr["upload_request_id"]]
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_URL"] = sanitize(
                            file_entry[FILE_ENTRIES_hdr["url"]]
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid]["FILE_UPLOAD_URL"] = (
                            sanitize(file_entry[FILE_ENTRIES_hdr["upload_url"]])
                        )
                        self.MESSAGES[uid][mid]["FILE"][fid][
                            "FILE_UPLOAD_SOURCE_PATH"
                        ] = file_entry[FILE_ENTRIES_hdr["upload_source_path"]]
                        self.MESSAGES[uid][mid]["FILE"][fid][
                            "FILE_THUMBNAIL_DIMENSIONS"
                        ] = f'{file_entry[FILE_ENTRIES_hdr["thumbnail_width"]]}x{file_entry[FILE_ENTRIES_hdr["thumbnail_height"]]}'
                        self.MESSAGES[uid][mid]["FILE"][fid][
                            "FILE_UPLOAD_USER_INITIATED"
                        ] = bool(file_entry[FILE_ENTRIES_hdr["user_initiated_upload"]])
            if (
                this_message[ZMRMESSAGE_hdr["ZREACTIONSEXIST"]] == 1
                and this_message[ZMRMESSAGE_hdr["ZREACTION"]] is not None
            ):
                reaction = this_message[ZMRMESSAGE_hdr["ZREACTION"]]
                for item in ZMRREACTIONITEM:
                    if (
                        item[ZMRREACTIONITEM_hdr["ZREACTION"]] == reaction
                        and item[ZMRREACTIONITEM_hdr["ZCOUNT"]] == 1
                    ):
                        reaction_emoji = item[ZMRREACTIONITEM_hdr["ZVALUE"]]
                        self.MESSAGES[uid][mid]["MESSAGE"]["REACTION"] = reaction_emoji
                        break
        return self.MESSAGES

    def get_files(self):
        ZMRGALLERYENTRY_hdr, ZMRGALLERYENTRY = split_table(
            self.AGENT["ZMRGALLERYENTRY"]
        )
        fileIdx = get_indices(self.FILES["file"][0])
        fileData = self.FILES["file"][1:]
        for file in fileData:
            file_id = file[fileIdx["file_id"]]
            self.FILE_DATA[file_id] = {}
            self.FILE_DATA[file_id]["FILE_NAME"] = file[fileIdx["filename"]]
            self.FILE_DATA[file_id]["FILE_SIZE"] = file[fileIdx["filesize"]]
            self.FILE_DATA[file_id]["FILE_TYPE"] = file[fileIdx["mimetype"]]
            self.FILE_DATA[file_id]["FILE_ALT_TEXT"] = file[fileIdx["alt_text"]]
            self.FILE_DATA[file_id]["FILE_DURATION"] = file[fileIdx["duration"]]
            self.FILE_DATA[file_id]["FILE_ORIGINAL_CONTENT_FILENAME"] = file[
                fileIdx["original_content_filename"]
            ]
            self.FILE_DATA[file_id]["FILE_STORAGE_CONTENT_FILENAME"] = file[
                fileIdx["storage_content_filename"]
            ]
            self.FILE_DATA[file_id]["FILE_STORAGE_PREVIEW_FILENAME"] = file[
                fileIdx["storage_preview_filename"]
            ]
            if self.FILE_CACHES:
                for cache in self.FILE_CACHES:
                    fp = os.path.abspath(
                        f'{cache}{os.sep}{file[fileIdx["original_content_filename"]]}'
                    )
                    if os.path.exists(fp) and os.path.isfile(fp):
                        self.FILE_DATA[file_id][
                            "FILE_ORIGINAL_CONTENT_FILENAME_EXISTS"
                        ] = fp
                    fp = os.path.abspath(
                        f'{cache}{os.sep}{file[fileIdx["storage_content_filename"]]}'
                    )
                    if os.path.exists(fp) and os.path.isfile(fp):
                        self.FILE_DATA[file_id][
                            "FILE_STORAGE_CONTENT_FILENAME_EXISTS"
                        ] = fp
                    fp = os.path.abspath(
                        f'{cache}{os.sep}{file[fileIdx["storage_preview_filename"]]}'
                    )
                    if os.path.exists(fp) and os.path.isfile(fp):
                        self.FILE_DATA[file_id][
                            "FILE_STORAGE_PREVIEW_FILENAME_EXISTS"
                        ] = fp
            self.FILE_DATA[file_id]["FILE_STORAGE_SMALLEST_PREVIEW_FILENAME"] = file[
                fileIdx["storage_smallest_preview_filename"]
            ]
            self.FILE_DATA[file_id]["FILE_UPLOAD_SOURCE_PATH"] = file[
                fileIdx["upload_source_path"]
            ]
            self.FILE_DATA[file_id]["FILE_UPLOAD_URL"] = sanitize(
                file[fileIdx["upload_url"]]
            )
            self.FILE_DATA[file_id]["FILE_URL"] = sanitize(file[fileIdx["url"]])
            self.FILE_DATA[file_id]["FILE_PREVIEW_URL"] = sanitize(
                file[fileIdx["preview_url"]]
            )
            self.FILE_DATA[file_id]["FILE_CONTENT_URL"] = sanitize(
                file[fileIdx["content_url"]]
            )
        for entry in ZMRGALLERYENTRY:
            file_id = entry[ZMRGALLERYENTRY_hdr["ZFILEID"]]
            if file_id not in self.FILE_DATA:
                self.FILE_DATA[file_id] = {}
            self.FILE_DATA[file_id]["FILE_MESSAGE_ID"] = entry[
                ZMRGALLERYENTRY_hdr["ZMESSAGEID"]
            ]
            self.FILE_DATA[file_id]["FILE_TIME"] = convert_unix_ts(
                entry[ZMRGALLERYENTRY_hdr["ZTIME"]]
            )
            self.FILE_DATA[file_id]["FILE_CAPTION"] = entry[
                ZMRGALLERYENTRY_hdr["ZCAPTION"]
            ]
            self.FILE_DATA[file_id]["FILE_SENDER"] = entry[
                ZMRGALLERYENTRY_hdr["ZSENDERUID"]
            ]
            if "FILE_URL" in self.FILE_DATA[file_id] and self.FILE_DATA[file_id][
                "FILE_URL"
            ].startswith("hxxps://files.icq.net"):
                url_id = self.FILE_DATA[file_id]["FILE_URL"].split("/")[-1]
                file_type, file_timestamp, file_size = parse_file_id(url_id)
                self.FILE_DATA[file_id]["FILE_TYPE_FROM_URL"] = file_type
                self.FILE_DATA[file_id]["FILE_TIMESTAMP_FROM_URL"] = file_timestamp
                self.FILE_DATA[file_id]["FILE_METADATA_SIZE_FROM_URL"] = file_size
        return self.FILE_DATA

    def get_uid(self):
        if not self.ICQ_PLISTS:
            return None
        try:
            for plist in self.ICQ_PLISTS:
                if os.path.exists(plist) and not self.UID:
                    with open(plist, "rb") as plist_file:
                        plist_bytes = plist_file.read()
                        plist_content = plistlib.loads(plist_bytes)
                        if not self.UID:
                            if "mr_uid" in plist_content:
                                self.UID = plist_content["mr_uid"]
                            elif "PreservedApplicationUserID" in plist_content:
                                self.UID = plist_content["PreservedApplicationUserID"]
                            elif "ShareExtensionProfileUidKey" in plist_content:
                                self.UID = plist_content["ShareExtensionProfileUidKey"]
                            elif "LiveChatsHomeRequestDate" in plist_content:
                                [uid_key] = plist_content[
                                    "LiveChatsHomeRequestDate"
                                ].keys()
                                self.UID = uid_key.split("|")[0]
                            elif "PreviousProfilePIDKey" in plist_content:
                                self.UID = plist_content["PreviousProfilePIDKey"].split(
                                    "_"
                                )[0]
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return self.UID

    def correlate_data(self):
        msgs_sent = msgs_rcvd = 0
        total_rcvd = 0
        total_sent = 0
        total_all = 0
        for uid, content in self.MESSAGES.items():
            for _, msg in content.items():
                if msg["MESSAGE"]["DIRECTION"] == "OUTGOING":
                    msgs_sent += 1
                elif msg["MESSAGE"]["DIRECTION"] == "INCOMING":
                    msgs_rcvd += 1
            self.CONTACTS[uid]["MessagesSent"] = msgs_sent
            self.CONTACTS[uid]["MessagesReceived"] = msgs_rcvd
            self.CONTACTS[uid]["MessagesTotal"] = msgs_sent + msgs_rcvd
            total_rcvd += msgs_rcvd
            total_sent += msgs_sent
            msgs_sent = msgs_rcvd = 0
        total_all = total_sent + total_rcvd
        self.OWNER["TOTAL_SENT"] = total_sent
        self.OWNER["TOTAL_RCVD"] = total_rcvd
        self.OWNER["TOTAL_ALL"] = total_all

    def parse_db(self, db_file):
        conn = sqlite3.connect(f"file:///{db_file}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        db_tables = {}
        for (table_name,) in tables:
            db_tables[table_name] = []
            cursor.execute(f"SELECT * FROM {table_name}")
            names = [description[0] for description in cursor.description]
            db_tables[table_name].append(names)
            tbl_content = cursor.fetchall()
            for row in tbl_content:
                db_tables[table_name].append(row)
        return db_tables


def convert_tiktok_ts(ts):
    result = ""
    unix_ts = int(ts) >> 32
    if unix_ts < 32536799999:
        result = dt.fromtimestamp(float(unix_ts), timezone.utc).strftime(__fmt__)
    return result


def convert_nsdate_ts(ts):
    result = ""
    epoch_2001 = dt(2001, 1, 1, tzinfo=timezone.utc)
    dt_obj = epoch_2001 + timedelta(seconds=float(ts))
    result = dt_obj.strftime(__fmt__)
    return result


def convert_unix_ts(ts):
    result = dt.fromtimestamp(ts, timezone.utc).strftime(__fmt__)
    return result


def convert_long_unix_ts(ts):
    ts = int(ts)
    result = dt.fromtimestamp(float(ts / 1000000) / 1000.0, timezone.utc).strftime(
        f"{__fmt__}.%f"
    )
    return result


def get_indices(row):
    indices = {}
    for each in row:
        idx = row.index(each)
        indices[each] = idx
    return indices


def split_table(db_table):
    idx = get_indices(db_table[0])
    data = []
    try:
        data = db_table[1:]
    except IndexError:
        pass
    return idx, data


def start_web(IP, load_dir, links=False, printing=False, device=None):
    @icqweb.app.route("/static/<path:filename>")
    def custom_static(filename):
        return send_from_directory(load_dir, filename)

    icqweb.app.config["load"] = load_dir
    with open(f"{load_dir}contacts.json", encoding="utf-8") as json_contacts:
        contacts = json.load(json_contacts)
    with open(f"{load_dir}messages.json", encoding="utf-8") as json_messages:
        messages = json.load(json_messages)
    with open(f"{load_dir}owner.json", encoding="utf-8") as json_owner:
        owner = json.load(json_owner)
    with open(f"{load_dir}files.json", encoding="utf-8") as json_files:
        files = json.load(json_files)
    icqweb.app.config["CONTACTS"] = contacts
    icqweb.app.config["MESSAGES"] = messages
    icqweb.app.config["OWNER"] = owner
    icqweb.app.config["LINKS"] = links
    icqweb.app.config["FILES"] = files
    icqweb.app.config["DEVICE"] = device
    icqweb.app.config["SERVER_NAME"] = f"{IP}:5000"
    icqweb.app.config["APPLICATION_ROOT"] = "/"
    icqweb.app.config["PREFERRED_URL_SCHEME"] = "http"
    icqweb.app.config["PRINTING"] = printing
    icqweb.app.config["TESTING"] = False
    if not printing:
        icqweb.app.config["SEARCH_INDEX"] = build_search_index(icqweb.app)
    cli = sys.modules["flask.cli"]
    cli.show_server_banner = lambda *x: None
    icqweb.app.run(host=IP, debug=False, use_reloader=False)


def generate_pdf(pages, logger=None):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for entry in pages:
            url, pdf_path = entry
            if logger:
                logger.info(f"Generating {pdf_path}")
            page.goto(url, wait_until="networkidle")
            try:
                page.pdf(
                    path=pdf_path,
                    format="A4",
                    print_background=True,
                    landscape=True,
                    prefer_css_page_size=True,
                )
            except PermissionError:
                if logger:
                    logger.error(
                        f"Unable to write to {pdf_path} - either the file is open, or sufficient permissions are not provided."
                    )
                else:
                    print(
                        f"Unable to write to {pdf_path} - either the file is open, or sufficient permissions are not provided."
                    )
        if logger:
            logger.info("PDF Generation complete, closing browser.")
        browser.close()


def print_to_pdf(base_url, output, logger=None):
    global PDFS
    pages = []
    filename = f"{output}{os.sep}ICQ Contacts.pdf"
    pages.append((base_url, filename))
    PDFS.append(filename)
    html = requests.get(base_url, timeout=10).text
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("a"):
        href = link.get("href")
        if href and href.startswith("/"):
            full_url = base_url + href
            filename = href.strip("/").replace("/", "_") or "index"
            if "IgnoreList" in filename:  ## TODO - include ignorelist as page
                continue
            if "_" in filename:
                split = filename.split("_")
                filename = f"{split[1]}_{split[0]}"
            if filename.endswith(".html"):
                filename = filename.replace(".html", "")
            pages.append((full_url, f"{output}{os.sep}{filename}.pdf"))
            PDFS.append(f"{output}{os.sep}{filename}.pdf")
    generate_pdf(pages, logger)


def merge_pdfs(output):
    merger = PdfWriter()
    for pdf in PDFS:
        merger.append(pdf)
    merger.write(output)
    merger.close()


def wait_for_server(url):
    for _ in range(30):
        try:
            r = requests.get(url, timeout=1)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("[!] Web server did not appear to start")


def install_chromium():
    if getattr(sys, "frozen", False):
        py_dir = os.path.join(sys._MEIPASS, "python.exe")
    else:
        py_dir = sys.executable
    try:
        subprocess.check_call([py_dir, "-m", "playwright", "install", "chromium"])
    except Exception as exc:
        raise RuntimeError("[!] Unable to install Chromium Browser") from exc


def save_output(content, filename):
    with open(filename, "w", encoding="utf-8") as json_file:
        json.dump(content, json_file)


def log_output(log_path, to_file=False):
    now = dt.now().strftime("%Y%m%d-%H%M%S")
    log = logging.getLogger("icq-parser")
    log.setLevel(logging.DEBUG)
    log_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt=__fmt__,
    )
    log_file = f"{log_path}{os.sep}icq-parser-{now}.txt"
    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(log_fmt)
    log.addHandler(stream_handler)
    if to_file:
        file_handler = logging.FileHandler(log_file, "w", "utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(log_fmt)
        log.addHandler(file_handler)
    return log


def main():
    arg_parse = argparse.ArgumentParser(
        description="ICQ Parser for iOS and Desktop artifacts"
    )
    subparsers = arg_parse.add_subparsers(dest="action", required=True)
    install_parser = subparsers.add_parser(
        "install", help="Install Playwright Dependency for PDF printing"
    )
    process_parsers = subparsers.add_parser(
        "process", help="Process iOS and Desktop ICQ databases"
    )
    process_subparser = process_parsers.add_subparsers(
        dest="device", title="Device Type"
    )
    load_parser = subparsers.add_parser(
        "load",
        help="Load data for display into the web interace",
    )
    load_device = load_parser.add_subparsers(dest="device", title="Device Type")
    ios_process = process_subparser.add_parser("ios", help="process iOS artifacts")
    desktop_process = process_subparser.add_parser(
        "desktop", help="process desktop artifacts"
    )
    save_parser = subparsers.add_parser(
        "save", help="Save the Flask website locally for offline viewing"
    )
    save_device = save_parser.add_subparsers(dest="device", title="Device Type")
    ios_process.add_argument(
        "-s",
        "--source",
        metavar="<PATH>",
        help="source path for the iOS ICQ artifacts",
        required=True,
    )
    ios_process.add_argument(
        "-d",
        "--dest",
        metavar="<PATH>",
        help="destination path to save the processing output",
        required=True,
    )
    ios_process.add_argument(
        "--log",
        action="store_true",
        help="generates a log file",
    )
    desktop_process.add_argument(
        "-s",
        "--source",
        metavar="<PATH>",
        help="source path for the ICQ Desktop artifacts",
        required=True,
    )
    desktop_process.add_argument(
        "-d",
        "--dest",
        metavar="<PATH>",
        help="destination path to save the processing output",
        required=True,
    )
    desktop_process.add_argument(
        "--log",
        action="store_true",
        help="generates a log file",
    )
    ios_load = load_device.add_parser("ios", help="load iOS artifacts")
    desktop_load = load_device.add_parser("desktop", help="load desktop artifacts")
    ios_load.add_argument(
        "-i",
        "--ip",
        metavar="<IP>",
        help="IPv4 address to use for the web interface",
        default="127.0.0.1",
    )
    ios_load.add_argument(
        "-l",
        "--links",
        help="Enable clickable links.",
        action="store_true",
        default=False,
    )
    ios_load.add_argument(
        "-p",
        "--print",
        help="Path to store the PDF-printed webpages.",
        metavar="<PATH>",
    )
    ios_load.add_argument(
        "-m",
        "--merge",
        help="Merge PDFs into a single PDF, requires -p/--print",
        action="store_true",
    )
    ios_load.add_argument(
        "-s",
        "--source",
        metavar="<PATH>",
        help="Path to the source JSON files to display in the web interface",
        required=True,
    )
    ios_load.add_argument(
        "--debug",
        help="Rapid relaunch of script for debugging without indexing",
        action="store_true",
    )
    ios_load.add_argument(
        "--log",
        action="store_true",
        help="generates a log file",
    )
    desktop_load.add_argument(
        "-i",
        "--ip",
        metavar="<IP>",
        help="IPv4 address to use for the web interface",
        default="127.0.0.1",
    )
    desktop_load.add_argument(
        "-l",
        "--links",
        help="Enable clickable links.",
        action="store_true",
        default=False,
    )
    desktop_load.add_argument(
        "-p",
        "--print",
        help="Path to store the PDF-printed webpages.",
        metavar="<PATH>",
    )
    desktop_load.add_argument(
        "-m",
        "--merge",
        help="Merge PDFs into a single PDF, requires -p/--print",
        action="store_true",
    )
    desktop_load.add_argument(
        "-s",
        "--source",
        metavar="<PATH>",
        help="Path to the source JSON files to display in the web interface",
        required=True,
    )
    desktop_load.add_argument(
        "--debug",
        help="Rapid relaunch of script for debugging without indexing",
        action="store_true",
    )
    desktop_load.add_argument(
        "--log",
        action="store_true",
        help="generates a log file",
    )
    ios_save = save_device.add_parser("ios", help="save iOS webpage")
    ios_save.add_argument(
        "-i",
        "--ip",
        metavar="<IP>",
        help="IPv4 address to use for the web interface",
        default="127.0.0.1",
    )
    ios_save.add_argument(
        "-l",
        "--links",
        help="Enable clickable links.",
        action="store_true",
        default=False,
    )
    ios_save.add_argument(
        "-s",
        "--source",
        metavar="<PATH>",
        help="Path to the source JSON files to display in the web interface",
        required=True,
    )
    ios_save.add_argument(
        "-d",
        "--dest",
        metavar="<PATH>",
        help="Location to save the downloaded webpage",
        required=True,
    )
    ios_save.add_argument(
        "--log",
        action="store_true",
        help="generates a log file",
    )
    desktop_save = save_device.add_parser("desktop", help="save desktop webpage")
    desktop_save.add_argument(
        "-i",
        "--ip",
        metavar="<IP>",
        help="IPv4 address to use for the web interface",
        default="127.0.0.1",
    )
    desktop_save.add_argument(
        "-l",
        "--links",
        help="Enable clickable links.",
        action="store_true",
        default=False,
    )
    desktop_save.add_argument(
        "-s",
        "--source",
        metavar="<PATH>",
        help="Path to the source JSON files to display in the web interface",
        required=True,
    )
    desktop_save.add_argument(
        "-d",
        "--dest",
        metavar="<PATH>",
        help="Location to save the downloaded webpage",
        required=True,
    )
    desktop_save.add_argument(
        "--log",
        action="store_true",
        help="generates a log file",
    )
    args = arg_parse.parse_args()
    if args.action == "install":
        print(
            "[-] Attempting to install the chromium headless browser requirement for Playwright"
        )
        install_chromium()
        print("[+] Install complete.")
    if args.action == "process" and args.device == "ios":
        if not os.path.exists(args.source) and not os.path.isdir(args.source):
            arg_parse.error(
                f"The path {args.source} does not exist or is not a directory."
            )
        if not os.path.exists(args.dest) and not os.path.isdir(args.dest):
            arg_parse.error(
                f"The output path {args.dest} does not exist or is not a directory."
            )
        dest_path = os.path.abspath(args.dest)
        logger = log_output(dest_path, args.log)
        start = dt.now().strftime(__fmt__)
        logger.info(f"Processing start time: {start}")
        logger.info(f"Identifying content in {os.path.abspath(args.source)} ...")
        iparser = iOSParser(os.path.normpath(os.path.abspath(args.source)))
        logger.info("Processing owner info ...")
        logger.info(f"Owner UID found: {iparser.UID}")
        logger.info(
            f"Attempting to create {os.path.normpath(os.path.abspath(dest_path))}{os.sep}{iparser.UID}"
        )
        out_path = (
            f"{os.path.normpath(os.path.abspath(dest_path))}{os.sep}{iparser.UID}"
        )
        try:
            os.mkdir(out_path)
            dest_path = out_path
            logger.info(f"Path created. Full output path will now be {dest_path}.")
        except PermissionError:
            logger.warning(
                f"Unable to create folder {iparser.UID} in folder {dest_path}.\n"
                f"Defaulting back to {dest_path} as the destination."
            )
        except FileExistsError:
            if os.path.isdir(out_path):
                logger.warning(
                    f"A folder named for the Owner UID ({iparser.UID}) exists in {dest_path} and will be used for output."
                )
                dest_path = out_path
            else:
                logger.warning(
                    f"A file named {iparser.UID} exists in {dest_path}, so sub-folder {iparser.UID} will NOT be created."
                )
        if iparser.FILES_DB != "":
            logger.info(f"Extracting data from {iparser.FILES_DB}")
            iparser.FILES = iparser.parse_db(iparser.FILES_DB)
        if iparser.AGENT_DB != "":
            logger.info(f"Extracting data from {iparser.AGENT_DB}")
            iparser.AGENT = iparser.parse_db(iparser.AGENT_DB)
        if iparser.CL_DB != "":
            logger.info(f"Extracting data from {iparser.CL_DB}")
            iparser.CL = iparser.parse_db(iparser.CL_DB)
        if iparser.SHARED_DB != "":
            logger.info(f"Extracting data from {iparser.SHARED_DB}")
            iparser.SHARED = iparser.parse_db(iparser.SHARED_DB)
        if iparser.CL:
            logger.info("Processing contacts ...")
            iparser.get_contacts()
        if iparser.AGENT:
            logger.info("Processing messages ...")
            iparser.get_messages()
        if iparser.FILES:
            logger.info("Processing shared files ...")
            iparser.get_files()
        iparser.correlate_data()
        outputs = {
            "files": iparser.FILE_DATA,
            "messages": iparser.MESSAGES,
            "contacts": iparser.CONTACTS,
            "owner": iparser.OWNER,
        }
        for file, data in outputs.items():
            if data:
                save_output(data, f"{dest_path}{os.sep}{file}.json")
                logger.info(f"{dest_path}{os.sep}{file}.json saved.")
        end = dt.now().strftime(__fmt__)
        time_taken = str(
            timedelta(
                seconds=(
                    dt.strptime(end, __fmt__) - dt.strptime(start, __fmt__)
                ).seconds
            )
        )
        logger.info(f"Processing end time: {end}.")
        logger.info(f"Processing time: {time_taken}.")
        logger.info(f"Files saved to {dest_path}")
    if args.action == "process" and args.device == "desktop":
        if os.path.exists(args.dest) and os.path.isdir(args.dest):
            dest_path = os.path.abspath(args.dest)
            logger = log_output(dest_path, args.log)
            logger.info(f"Output path: {dest_path}")
        else:
            arg_parse.error(
                f"[!] the path {args.dest} does not exist. Please check your path and try again."
            )
        if os.path.exists(args.source) and os.path.isdir(args.source):
            start = dt.now().strftime(__fmt__)
            logger.info(f"Processing start time: {start}")
            logger.info(f"Identifying content in {os.path.abspath(args.source)} ...")
            dparser = DesktopParser(os.path.abspath(args.source))
        else:
            arg_parse.error(
                f"[!] The path {args.source} does not exist. Please check your path and try again."
            )

        if dparser.INFO_CACHE_FILES:
            logger.info("Processing users profile information cache ... ")
            dparser.get_info_cache()
            if dparser.OWNER_UID:
                uid_path = f"{dest_path}{os.sep}{dparser.OWNER_UID}"
                try:
                    os.mkdir(uid_path)
                    dest_path = uid_path
                    logger.info(
                        f"Owner UID found. Full output path will now be {dest_path}."
                    )
                except PermissionError:
                    logger.warning(
                        f"Unable to create folder {dparser.OWNER_UID} in folder {dest_path}.\n"
                        f"Defaulting back to {dest_path} as the destination."
                    )
                except FileExistsError:
                    if os.path.isdir(uid_path):
                        logger.warning(
                            f"A folder named for the Owner UID ({dparser.OWNER_UID}) exists in {dest_path} and will be used for output."
                        )
                        dest_path = uid_path
                    else:
                        logger.warning(
                            f"A file named {dparser.OWNER_UID} exists in {dest_path}, so sub-folder {dparser.OWNER_UID} will NOT be created."
                        )
        if dparser.DIALOG_STATE_FILES:
            logger.info("Processing dialog states ... ")
            dparser.get_dlg_state()
        if dparser.CALL_LOG_CACHE:
            logger.info("Processing call logs ... ")
            dparser.get_call_log()
        if dparser.DIALOGS_FILES:
            logger.info("Processing dialogs ... ")
            dparser.get_dialogs()
        if dparser.DRAFT_FILES:
            logger.info("Processing draft messages ... ")
            dparser.get_drafts()
        if dparser.GALLERY_CACHE_FILES:
            logger.info("Processing gallery cache for shared files ... ")
            dparser.get_shared_files()
        if dparser.GALLERY_STATE_FILES:
            logger.info("Processing gallery state files for each contact ... ")
            dparser.get_gallery_state()
        if dparser.HISTORY_FILES:
            logger.info("Processing message search history ... ")
            dparser.get_msg_search_history()
        if dparser.CONTACT_LIST_CACHE:
            logger.info("Processing contact list ... ")
            dparser.get_contact_list()
        if dparser.DB_FILES:
            if len(dparser.DB_FILES) < 15:
                logger.info("Processing messages ... ")
            else:
                logger.info(
                    f"Processing messages for {len(dparser.DB_FILES)} users, this may take some time. Please be patient ... "
                )
            dparser.get_db_content()
        dparser.correlate_data()
        outputs = {
            "owner": dparser.INFO_CACHE,
            "calls": dparser.CALL_LOG,
            "dialogs": dparser.DIALOGS,
            "dialog-states": dparser.DIALOG_STATES,
            "drafts": dparser.DRAFTS,
            "files": dparser.SHARED_FILES,
            "states": dparser.GALLERY_STATE,
            "history": dparser.SEARCH_HISTORY,
            "messages": dparser.MESSAGES,
            "contacts": dparser.CONTACT_LIST,
        }
        for file, data in outputs.items():
            if data:
                save_output(data, f"{dest_path}{os.sep}{file}.json")
                logger.info(f"{dest_path}{os.sep}{file}.json saved.")
        end = dt.now().strftime(__fmt__)
        time_taken = str(
            timedelta(
                seconds=(
                    dt.strptime(end, __fmt__) - dt.strptime(start, __fmt__)
                ).seconds
            )
        )
        logger.info(f"Processing end time: {end}.")
        logger.info(f"Processing time: {time_taken}.")
        logger.info(f"Files saved to {dest_path}")
    if args.action == "load" and args.device in {"ios", "desktop"}:
        load_path = None
        try:
            ipaddress.ip_address(args.ip)
        except ValueError:
            arg_parse.error(f"[!] The IP address {args.ip} is not a valid IP address")
        if not (
            os.path.exists(f"{args.source}{os.sep}contacts.json")
            and os.path.exists(f"{args.source}{os.sep}messages.json")
            and os.path.exists(f"{args.source}{os.sep}owner.json")
        ):
            arg_parse.error(
                f"[!] The path {args.source} does not exist, or one or all of the files contact/messages/owner.json are not present in the directory."
            )
        load_path = f"{os.path.normpath(os.path.abspath(args.source))}{os.sep}"
        if args.print and os.path.exists(args.print) and os.path.isdir(args.print):
            log_path = f"{os.path.normpath(os.path.abspath(args.print))}{os.sep}"
        else:
            log_path = load_path
        logger = log_output(log_path, args.log)
        logger.info(f"Loading json files from {load_path}")
        if args.merge and not args.print:
            arg_parse.error(
                "[!] The -m/--merge option requires -p/--print for PDFs to be generated first.\nMake sure you include both of these to merge the PDFs."
            )
        if args.print and os.path.exists(args.print) and os.path.isdir(args.print):
            output_path = f"{os.path.normpath(os.path.abspath(args.print))}"
            url = f"http://{args.ip}:5000"
            try:
                printing = True
                logger.info(f"Starting the webserver at {url}")
                flask_thread = threading.Thread(
                    target=start_web,
                    args=(args.ip, load_path, args.links, printing, args.device),
                    daemon=True,
                )
                flask_thread.start()
            except Exception as e:
                arg_parse.error(f"[!] Unable to start the web server: {e}")
            wait_for_server(url)
            logger.info("Gathering list of PDFs to generate ...")
            print_to_pdf(url, output_path, logger)
            if args.merge:
                merge_pdfs(f"{output_path}{os.sep}ICQ-Content-Combined.pdf")
        else:
            url = f"http://{args.ip}:5000"
            try:
                logger.info("Indexing content for search ...")
                logger.info(f"Starting the webserver at {url}")
                start_web(
                    args.ip,
                    load_path,
                    links=args.links,
                    printing=args.debug,
                    device=args.device,
                )
            except Exception as e:
                arg_parse.error(f"[!] Unable to start the web server: {e}")
    if args.action == "save" and args.device in {"ios", "desktop"}:
        source_path = None
        dest_path = None
        try:
            ipaddress.ip_address(args.ip)
        except ValueError:
            arg_parse.error(f"The IP address {args.ip} is not a valid IP address")
        url = f"http://{args.ip}:5000"
        if not (
            os.path.exists(f"{args.source}{os.sep}contacts.json")
            and os.path.exists(f"{args.source}{os.sep}messages.json")
            and os.path.exists(f"{args.source}{os.sep}owner.json")
        ):
            arg_parse.error(
                f"[!] The path {args.source} does not exist, or one or all of the files contact/messages/owner.json are not present in the directory."
            )
        source_path = f"{os.path.normpath(os.path.abspath(args.source))}{os.sep}"
        if args.dest and not (os.path.exists(args.dest) and os.path.isdir(args.dest)):
            arg_parse.error(
                f"[!] The path {args.dest} does not exist for output. Please check your path and try again"
            )
        dest_path = f"{os.path.normpath(os.path.abspath(args.dest))}"
        logger = log_output(dest_path, args.log)
        logger.info(f"Loading json files from {source_path}.")
        try:
            saving = True
            logger.info(f"Starting the webserver at {url}")
            flask_thread = threading.Thread(
                target=start_web,
                args=(args.ip, source_path, args.links, saving, args.device),
                daemon=True,
            )
            flask_thread.start()
        except Exception as e:
            arg_parse.error(f"[!] Unable to start the web server: {e}")
        wait_for_server(url)
        try:
            save_website(
                url,
                project_folder=dest_path,
                project_name="ICQ_PARSER_WEBSITE_OUTPUT",
                bypass_robots=True,
                debug=False,
                threaded=False,
                delay=None,
                open_in_browser=False,
            )
        except Exception as e:
            logger.error(f"Unable to save website: {e}")
        logger.info(f"Website download finished - saved to {dest_path}.")
        flask_thread._tstate_lock.release_lock()
        flask_thread._stop()
        logger.info("Web server stopped.")


if __name__ == "__main__":
    main()
