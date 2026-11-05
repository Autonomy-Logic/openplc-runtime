# GPIOD Plugin for OpenPLC

A native plugin for OpenPLC that provides GPIO (General Purpose Input/Output) support using the `libgpiod` library.
It can be used on any Linux device with GPIOs, not only on RaspberryPi, even on PC with USB-GPIO devices

## Dependencies

### Required Packages
To compile this plugin, you must have the `libgpiod-dev` package installed:

```bash
# On Debian/Ubuntu
sudo apt-get install libgpiod-dev

# On Fedora/RHEL
sudo dnf install libgpiod-devel
```

## Building

```bash
make
```

## Configuration

The plugin requires a CSV configuration file that maps IEC addresses to GPIO lines. The file path is specified in the OpenPLC plugin configuration.

### CSV File Format

The CSV file contains three columns: `IEC_ADDRESS`, `CHIP_NAME`, `LINE_IDENTIFIER`

**Header (required):**
```
IEC_ADDRESS,CHIP_NAME,LINE_IDENTIFIER
```

**Field Descriptions:**
- **IEC_ADDRESS**: IEC 61131-3 format address
  - Input format: `%IX<byte>.<bit>` (e.g., `%IX0.0`)
  - Output format: `%QX<byte>.<bit>` (e.g., `%QX0.1`)
  - `<byte>`: Buffer index (0-1023)
  - `<bit>`: Bit position within byte (0-7)

- **CHIP_NAME**: GPIO chip device path
  - Typically `/dev/gpiochip0`, `/dev/gpiochip1`, etc.
  - Found in `/dev/*` directory

- **LINE_IDENTIFIER**: GPIO line reference
  - Can be a numeric offset (e.g., `0`, `5`, `17`)
  - Or a named line (e.g., `LED_STATUS`, `BUTTON_RESET`)
  - Named lines are resolved via the chip's line names information

### Example I/O mapping CSV File

**File: io-map.csv**

```csv
IEC_ADDRESS,CHIP_NAME,LINE_IDENTIFIER
%IX0.2,/dev/gpiochip0,GPIO2
%IX0.3,/dev/gpiochip0,GPIO3
%IX0.4,/dev/gpiochip0,GPIO4
%IX0.3,/dev/gpiochip0,GPIO5
%QX0.0,/dev/gpiochip0,GPIO10
%QX0.1,/dev/gpiochip0,GPIO11
%QX0.2,/dev/gpiochip0,GPIO12
%QX0.3,/dev/gpiochip0,GPIO13
```

### Plugin Configuration

Add following line to **plugins.conf**
```csv
gpiod,./core/src/drivers/plugins/native/gpiod/gpiod_plugin.so,1,1,./config/io-map.csv
```

### Running plugin

When OpenPLC runtime is running mapped GPIO lines are controlled by OpenPLC, and consumer names are set
according given map configuration.

Here is an example for raspberrypi:
```bash
pi@raspberrypi:~ $ gpioinfo 
gpiochip0 - 54 lines:
	line   0:	"ID_SDA"        	input
	line   1:	"ID_SCL"        	input
	line   2:	"GPIO2"         	input consumer="OpenPLC-%IX0.2"
	line   3:	"GPIO3"         	input consumer="OpenPLC-%IX0.3"
	line   4:	"GPIO4"         	input consumer="OpenPLC-%IX0.4"
	line   5:	"GPIO5"         	input consumer="OpenPLC-%IX0.3"
	line   6:	"GPIO6"         	input
	line   7:	"GPIO7"         	input
	line   8:	"GPIO8"         	input
	line   9:	"GPIO9"         	input
	line  10:	"GPIO10"        	output consumer="OpenPLC-%QX0.0"
	line  11:	"GPIO11"        	output consumer="OpenPLC-%QX0.1"
	line  12:	"GPIO12"        	output consumer="OpenPLC-%QX0.2"
	line  13:	"GPIO13"        	output consumer="OpenPLC-%QX0.3"
	line  14:	"GPIO14"        	input
	line  15:	"GPIO15"        	input
	line  16:	"GPIO16"        	input
	line  17:	"GPIO17"        	input
	line  18:	"GPIO18"        	input
	line  19:	"GPIO19"        	input
	line  20:	"GPIO20"        	input
	line  21:	"GPIO21"        	input
	line  22:	"GPIO22"        	input
	line  23:	"GPIO23"        	input
	line  24:	"GPIO24"        	input
	line  25:	"GPIO25"        	input
	line  26:	"GPIO26"        	input
	line  27:	"GPIO27"        	input
	line  28:	"SDA0"          	input
	line  29:	"SCL0"          	input
	line  30:	"NC"            	input
	line  31:	"LAN_RUN"       	output
	line  32:	"CAM_GPIO1"     	output
	line  33:	"NC"            	input
	line  34:	"NC"            	input
	line  35:	"PWR_LOW_N"     	input consumer="PWR"
	line  36:	"NC"            	input
	line  37:	"NC"            	input
	line  38:	"USB_LIMIT"     	output
	line  39:	"NC"            	input
	line  40:	"PWM0_OUT"      	input
	line  41:	"CAM_GPIO0"     	output consumer="cam1_regulator"
	line  42:	"NC"            	input
	line  43:	"NC"            	input
	line  44:	"ETH_CLK"       	input
	line  45:	"PWM1_OUT"      	input
	line  46:	"HDMI_HPD_N"    	input active-low consumer="hpd"
	line  47:	"STATUS_LED"    	output consumer="ACT"
	line  48:	"SD_CLK_R"      	input
	line  49:	"SD_CMD_R"      	input
	line  50:	"SD_DATA0_R"    	input
	line  51:	"SD_DATA1_R"    	input
	line  52:	"SD_DATA2_R"    	input
	line  53:	"SD_DATA3_R"    	input
```
