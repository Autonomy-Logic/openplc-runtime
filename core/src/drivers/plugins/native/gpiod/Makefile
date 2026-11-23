# Makefile for test plugin

CC = gcc
CFLAGS = -fPIC -I../../../../lib -I../../../../drivers -I..
LDFLAGS = -shared -lpthread -lgpiod

TARGET = gpiod_plugin.so
SOURCE = gpiod_plugin.c
PLUGIN_LOGGER = ../plugin_logger.c

LOADER_TARGET = test_plugin_loader
LOADER_SOURCE = test_plugin_loader.c

all: $(TARGET) $(LOADER_TARGET)

$(TARGET): $(SOURCE) $(PLUGIN_LOGGER)
	$(CC) $(CFLAGS) $(SOURCE) $(PLUGIN_LOGGER) $(LDFLAGS) -o $(TARGET)

$(LOADER_TARGET): $(LOADER_SOURCE)
	$(CC) -o $(LOADER_TARGET) $(LOADER_SOURCE) -ldl -lpthread -lgpiod

test: $(TARGET) $(LOADER_TARGET)
	./$(LOADER_TARGET)

clean:
	rm -f $(TARGET) $(LOADER_TARGET)

.PHONY: all clean test
