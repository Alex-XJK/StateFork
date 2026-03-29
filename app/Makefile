CC = g++
CFLAGS = -std=c++17 -O2 -Wall
LDFLAGS = -pthread

TARGET = rdb
OBJS = rdb.o

MEMHOG = memhog
MEMHOG_OBJS = memhog.o

FSGEN = fsgen
FSGEN_OBJS = fsgen.o

all: $(TARGET) $(MEMHOG) $(FSGEN)

$(TARGET):
$(OBJS):

$(MEMHOG): $(MEMHOG_OBJS)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

memhog.o: memhog.cpp
	$(CC) $(CFLAGS) -c -o $@ $<

$(FSGEN): $(FSGEN_OBJS)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

fsgen.o: fsgen.cpp
	$(CC) $(CFLAGS) -c -o $@ $<

PHONY += clean
PHONY += all

MEMTOUCH = memtouch
MEMTOUCH_OBJS = memtouch.o

all: $(MEMTOUCH)

$(MEMTOUCH): $(MEMTOUCH_OBJS)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

memtouch.o: memtouch.cpp
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f $(OBJS) $(TARGET) a.out $(MEMHOG_OBJS) $(MEMHOG) $(FSGEN_OBJS) $(FSGEN) $(MEMTOUCH_OBJS) $(MEMTOUCH)

.PHONY: $(PHONY)