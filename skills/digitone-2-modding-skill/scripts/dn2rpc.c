/* dn2rpc: talk to the Digitone II's MIDI RPC over USB (CoreMIDI, macOS). Read-only requests only.
 *   dn2rpc list                 MIDI ports
 *   dn2rpc ping                 opcode 0x01 (the device's name and opcode list)
 *   dn2rpc shot [FILE]          opcode 0x04 Screenshot: the decoded reply body to FILE (default /dev/stdout as hex)
 * Framing (upstream dnfw docs/midi-rpc.md, from elektroid): F0 00 20 3C 10 00 <encode87(body)> F7,
 * body = seq:2 BE, 00 00, opcode, payload; the reply's opcode = request | 0x80.
 * Build: clang -O2 -o dn2rpc dn2rpc.c -framework CoreMIDI -framework CoreFoundation */
#include <CoreMIDI/CoreMIDI.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static unsigned char rx[1 << 20];
static size_t rxn;
static volatile int rxdone;

static void name_of(MIDIObjectRef o, char *buf, size_t n)
{
    CFStringRef s = NULL;
    buf[0] = 0;
    if (MIDIObjectGetStringProperty(o, kMIDIPropertyDisplayName, &s) == noErr && s) {
        CFStringGetCString(s, buf, (CFIndex)n, kCFStringEncodingUTF8);
        CFRelease(s);
    }
}

static void on_read(const MIDIPacketList *pl, void *a, void *b)
{
    const MIDIPacket *p = &pl->packet[0];
    for (UInt32 i = 0; i < pl->numPackets; i++) {
        for (UInt16 j = 0; j < p->length; j++) {
            unsigned char c = p->data[j];
            if (c == 0xF0)
                rxn = 0;
            if (rxn < sizeof rx)
                rx[rxn++] = c;
            if (c == 0xF7 && rxn > 6 && rx[0] == 0xF0)
                rxdone = 1;
        }
        p = MIDIPacketNext(p);
    }
}

static size_t encode87(const unsigned char *s, size_t n, unsigned char *d)
{
    size_t o = 0;
    for (size_t i = 0; i < n; i += 7) {
        size_t m = n - i < 7 ? n - i : 7;
        unsigned char hi = 0;
        for (size_t k = 0; k < m; k++)
            hi |= (unsigned char)((s[i + k] >> 7) << (6 - k));
        d[o++] = hi;
        for (size_t k = 0; k < m; k++)
            d[o++] = s[i + k] & 0x7F;
    }
    return o;
}

static size_t decode87(const unsigned char *s, size_t n, unsigned char *d)
{
    size_t o = 0;
    for (size_t i = 0; i < n; i += 8) {
        unsigned char hi = s[i];
        size_t m = n - i - 1 < 7 ? n - i - 1 : 7;
        for (size_t k = 0; k < m; k++)
            d[o++] = (unsigned char)(s[i + 1 + k] | (((hi >> (6 - k)) & 1) << 7));
    }
    return o;
}

static int find(int dest, const char *want)
{
    ItemCount n = dest ? MIDIGetNumberOfDestinations() : MIDIGetNumberOfSources();
    char buf[256];
    for (ItemCount i = 0; i < n; i++) {
        MIDIEndpointRef e = dest ? MIDIGetDestination(i) : MIDIGetSource(i);
        name_of(e, buf, sizeof buf);
        if (strstr(buf, want))
            return (int)i;
    }
    return -1;
}

static int request(unsigned char op, const unsigned char *payload, size_t pn, unsigned char *body, size_t *bn, double wait)
{
    static MIDIClientRef cl;
    static MIDIPortRef in, out;
    static int src = -1, dst = -1;
    static unsigned short seq = 1;
    unsigned char b[600], msg[800];
    size_t n = 0, m;

    if (!cl) {
        MIDIClientCreate(CFSTR("dn2rpc"), NULL, NULL, &cl);
        MIDIInputPortCreate(cl, CFSTR("in"), on_read, NULL, &in);
        MIDIOutputPortCreate(cl, CFSTR("out"), &out);
        src = find(0, "Digitone");
        dst = find(1, "Digitone");
        if (src < 0 || dst < 0) {
            fprintf(stderr, "no Digitone MIDI port (run: dn2rpc list)\n");
            return -1;
        }
        MIDIPortConnectSource(in, MIDIGetSource((ItemCount)src), NULL);
    }
    b[n++] = (unsigned char)(seq >> 8); b[n++] = (unsigned char)seq; seq++;
    b[n++] = 0; b[n++] = 0; b[n++] = op;
    memcpy(b + n, payload, pn); n += pn;
    m = 0;
    msg[m++] = 0xF0; msg[m++] = 0x00; msg[m++] = 0x20; msg[m++] = 0x3C; msg[m++] = 0x10; msg[m++] = 0x00;
    m += encode87(b, n, msg + m);
    msg[m++] = 0xF7;
    rxdone = 0; rxn = 0;
    {
        Byte buf[1024];
        MIDIPacketList *pl = (MIDIPacketList *)buf;
        MIDIPacket *p = MIDIPacketListInit(pl);
        p = MIDIPacketListAdd(pl, sizeof buf, p, 0, m, msg);
        MIDISend(out, MIDIGetDestination((ItemCount)dst), pl);
    }
    for (int t = 0; t < (int)(wait * 1000) && !rxdone; t++)
        usleep(1000);
    if (!rxdone) {
        fprintf(stderr, "no reply (opcode %02x)\n", op);
        return -1;
    }
    *bn = decode87(rx + 6, rxn - 7, body);
    return 0;
}

int main(int argc, char **argv)
{
    static unsigned char body[1 << 20];
    size_t bn = 0;
    const char *cmd = argc > 1 ? argv[1] : "list";

    if (!strcmp(cmd, "list")) {
        char buf[256];
        for (ItemCount i = 0; i < MIDIGetNumberOfSources(); i++) {
            name_of(MIDIGetSource(i), buf, sizeof buf);
            printf("in  %lu %s\n", (unsigned long)i, buf);
        }
        for (ItemCount i = 0; i < MIDIGetNumberOfDestinations(); i++) {
            name_of(MIDIGetDestination(i), buf, sizeof buf);
            printf("out %lu %s\n", (unsigned long)i, buf);
        }
        return 0;
    }
    if (!strcmp(cmd, "ping")) {
        if (request(0x01, NULL, 0, body, &bn, 2.0))
            return 1;
        printf("reply %zu bytes, opcode %02x:", bn, bn > 4 ? body[4] : 0);
        for (size_t i = 0; i < bn; i++)
            printf(" %02x", body[i]);
        printf("\n");
        return 0;
    }
    if (!strcmp(cmd, "shot")) {
        if (request(0x04, NULL, 0, body, &bn, 3.0))
            return 1;
        if (argc > 2) {
            FILE *f = fopen(argv[2], "wb");
            fwrite(body, 1, bn, f);
            fclose(f);
            fprintf(stderr, "%zu bytes -> %s\n", bn, argv[2]);
        } else {
            for (size_t i = 0; i < bn; i++)
                printf("%02x%s", body[i], (i % 32 == 31) ? "\n" : " ");
            printf("\n");
        }
        return 0;
    }
    fprintf(stderr, "usage: dn2rpc list | ping | shot [FILE]\n");
    return 2;
}
