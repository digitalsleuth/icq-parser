    """
    Information for how this is used is found in
    im-desktop/gui/main_window/history_control/complex_message/FileSharingUtils.cpp
    """

    ## File URL details
    ## If the first character is between 0 and 7, it's an image
    ## Between 8 and F it's a video, but if it's D it's a video sticker
    ## Between I and J it's a PushToTalk audio
    ## IF it's L, it's a Lottie Sticker
    ## 4-5, GIF, but if it's 5 it's a GIF sticker
    ## 2 = IMAGE-Sticker
    ## Sample value: 0aokEGz4w1tywvoeDgAH475cec18cb1bg
    ## file_type = value[0] (0)
    ## duration or size (w/h) depending on file_type = value[1:5] (aokE)
    ## unknown "hash" = value[5:22] (Gz4w1tywvoeDgAH47)
    ## timestamp = value[22:30] (5cec18cb)
    ## Extension? = value[30:] (1bg)
    ## Length is at least 30, but default size is 33, as per im-desktop/common.shared/constants.h