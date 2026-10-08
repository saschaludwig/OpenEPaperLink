# Builds the optional native helper library (Group5 encoder + dithering)
# used by oepl_linux_ap.image.  Everything works without it, just slower.
CC ?= cc
CFLAGS ?= -O2 -fPIC -Wall -Wno-unused-function -Wno-unused-variable

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  SHARED_FLAGS = -dynamiclib
  TARGET = liboeplg5.dylib
else
  SHARED_FLAGS = -shared
  TARGET = liboeplg5.so
endif

SOURCES = g5_wrapper.c dither.c
G5_SOURCES = ../../ESP32_AP-Flasher/src/g5/g5enc.inl ../../ESP32_AP-Flasher/src/g5/Group5.h

all: $(TARGET)

$(TARGET): $(SOURCES) $(G5_SOURCES)
	$(CC) $(CFLAGS) $(SHARED_FLAGS) -o $@ $(SOURCES) -lm

clean:
	rm -f liboeplg5.so liboeplg5.dylib *.o

.PHONY: all clean
