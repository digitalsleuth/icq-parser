# ICQ Parser
This tool is designed to provide a web-based view of the contents of the ICQ chat application.

It also parses the contents into JSON files which can be ingested into other tool-sets.

## Note
An internet connection will be required on the first run to ensure that the "chromium" browser package is installed by playwright.

## Usage

### Install
```bash
icq-parser install
```

This will install the chromium headless version for the desired system. It is only required once, at initial run, and thus  
internet connectivity is required for this one time only.


### Process

```bash
usage: icq-parser process [-h] {ios,desktop} ...

options:
  -h, --help     show this help message and exit

Device Type:
  {ios,desktop}
    ios          process iOS artifacts
    desktop      process desktop artifacts
```

```bash
usage: icq-parser process ios [-h] -s <PATH> -d <PATH> [--log]

options:
  -h, --help            show this help message and exit
  -s <PATH>, --source <PATH>
                        source path for the iOS ICQ artifacts
  -d <PATH>, --dest <PATH>
                        destination path to save the processing output
  --log                 generates a log file
```

```bash
usage: icq-parser process desktop [-h] -s <PATH> -d <PATH> [--log]

options:
  -h, --help            show this help message and exit
  -s <PATH>, --source <PATH>
                        source path for the ICQ Desktop artifacts
  -d <PATH>, --dest <PATH>
                        destination path to save the processing output
  --log                 generates a log file
```

### Load

```bash
usage: icq-parser load [-h] {ios,desktop} ...

options:
  -h, --help     show this help message and exit

Device Type:
  {ios,desktop}
    ios          load iOS artifacts
    desktop      load desktop artifacts
```

```bash
usage: icq-parser load ios [-h] [-i <IP>] [-l] [-p <PATH>] [-m] -s <PATH>
                              [--debug] [--log]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -p <PATH>, --print <PATH>
                        Path to store the PDF-printed webpages.
  -m, --merge           Merge PDFs into a single PDF, requires -p/--print
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web
                        interface
  --debug               Rapid relaunch of script for debugging without
                        indexing
  --log                 generates a log file
```

```bash
usage: icq-parser load desktop [-h] [-i <IP>] [-l] [-p <PATH>] [-m] -s
                                  <PATH> [--debug] [--log]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -p <PATH>, --print <PATH>
                        Path to store the PDF-printed webpages.
  -m, --merge           Merge PDFs into a single PDF, requires -p/--print
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web
                        interface
  --debug               Rapid relaunch of script for debugging without
                        indexing
  --log                 generates a log file
```

### Save

```bash
usage: icq-parser save [-h] {ios,desktop} ...

options:
  -h, --help     show this help message and exit

Device Type:
  {ios,desktop}
    ios          save iOS webpage
    desktop      save desktop webpage
```

```bash
usage: icq-parser save ios [-h] [-i <IP>] [-l] -s <PATH> -d <PATH> [--log]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web
                        interface
  -d <PATH>, --dest <PATH>
                        Location to save the downloaded webpage
  --log                 generates a log file
```

```bash
usage: icq-parser save desktop [-h] [-i <IP>] [-l] -s <PATH> -d <PATH>
                                  [--log]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web
                        interface
  -d <PATH>, --dest <PATH>
                        Location to save the downloaded webpage
  --log                 generates a log file
```

