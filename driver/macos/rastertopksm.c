/*
 * rastertopksm: CUPS raster from macOS's cgpdftoraster -> the PBM input foo2zjs reads.
 *
 * macOS's CUPS sandbox keeps filters away from Homebrew's Ghostscript, so the queue lets
 * Apple's own rasterizer draw the page and this program only re-packs it:
 *   - the raster covers just the imageable area, so it is centred on a full-page canvas
 *     (foo2zjs then clips the same margins back off, as with Ghostscript);
 *   - every page becomes four P4 images in C, M, Y, K order: foo2zjs's "pksmraw" input;
 *   - 1-bit black (cupsColorSpace 3) fills only the K plane, and foo2zjs then sends a
 *     black-only page because no color dot is set;
 *   - 8-bit chunky CMYK (cupsColorSpace 6) is ordered-dithered into all four planes.
 *
 * Usage: rastertopksm WIDTH HEIGHT < raster > pbm   (WIDTH x HEIGHT = full page in dots)
 */
#include <cups/raster.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { CSPACE_K = 3, CSPACE_CMYK = 6, PLANES = 4, BLACK_PLANE = 3, MAX_DOTS = 20000 };

/* 8x8 Bayer matrix, scaled to 0..255 thresholds. */
static const unsigned char BAYER[8][8] = {
    {0, 128, 32, 160, 8, 136, 40, 168},     {192, 64, 224, 96, 200, 72, 232, 104},
    {48, 176, 16, 144, 56, 184, 24, 152},   {240, 112, 208, 80, 248, 120, 216, 88},
    {12, 140, 44, 172, 4, 132, 36, 164},    {204, 76, 236, 108, 196, 68, 228, 100},
    {60, 188, 28, 156, 52, 180, 20, 148},   {252, 124, 220, 92, 244, 116, 212, 84},
};

static void die(const char *message)
{
    fprintf(stderr, "ERROR: rastertopksm: %s\n", message);
    exit(1);
}

static void write_plane(const unsigned char *plane, unsigned width, unsigned height)
{
    printf("P4\n%u %u\n", width, height);
    if (fwrite(plane, (width + 7) / 8, height, stdout) != height)
        die("write failed");
}

static void set_dot(unsigned char *plane, unsigned bytes_per_row, unsigned x, unsigned y)
{
    plane[(size_t)y * bytes_per_row + x / 8] |= (unsigned char)(0x80 >> (x % 8));
}

int main(int argc, char **argv)
{
    if (argc != 3)
        die("usage: rastertopksm WIDTH HEIGHT");
    unsigned page_w = (unsigned)strtoul(argv[1], NULL, 10);
    unsigned page_h = (unsigned)strtoul(argv[2], NULL, 10);
    if (page_w == 0 || page_h == 0 || page_w > MAX_DOTS || page_h > MAX_DOTS)
        die("page size out of range");
    unsigned row_bytes = (page_w + 7) / 8;

    cups_raster_t *raster = cupsRasterOpen(0, CUPS_RASTER_READ);
    if (!raster)
        die("cannot open raster on stdin");

    cups_page_header2_t header;
    unsigned pages = 0;
    while (cupsRasterReadHeader2(raster, &header)) {
        int color = header.cupsColorSpace == CSPACE_CMYK;
        if (color ? header.cupsBitsPerPixel != 32 || header.cupsColorOrder != CUPS_ORDER_CHUNKED
                  : header.cupsColorSpace != CSPACE_K || header.cupsBitsPerPixel != 1)
            die("unsupported raster: need 1-bit K or 8-bit chunky CMYK");
        if (header.cupsWidth > page_w || header.cupsHeight > page_h)
            die("raster is larger than the page");

        unsigned left = (page_w - header.cupsWidth) / 2;
        unsigned top = (page_h - header.cupsHeight) / 2;
        size_t plane_size = (size_t)row_bytes * page_h;
        unsigned char *canvas = calloc(PLANES, plane_size);
        unsigned char *line = malloc(header.cupsBytesPerLine);
        if (!canvas || !line)
            die("out of memory");

        for (unsigned y = 0; y < header.cupsHeight; y++) {
            if (cupsRasterReadPixels(raster, line, header.cupsBytesPerLine) != header.cupsBytesPerLine)
                die("short raster line");
            unsigned py = top + y;
            for (unsigned x = 0; x < header.cupsWidth; x++) {
                unsigned px = left + x;
                if (!color) {
                    if (line[x / 8] & (0x80 >> (x % 8)))
                        set_dot(canvas + BLACK_PLANE * plane_size, row_bytes, px, py);
                    continue;
                }
                unsigned char threshold = BAYER[py % 8][px % 8];
                for (unsigned p = 0; p < PLANES; p++)
                    if (line[x * PLANES + p] > threshold)
                        set_dot(canvas + p * plane_size, row_bytes, px, py);
            }
        }
        for (unsigned p = 0; p < PLANES; p++)
            write_plane(canvas + p * plane_size, page_w, page_h);
        free(canvas);
        free(line);
        pages++;
    }
    cupsRasterClose(raster);
    if (pages == 0)
        die("no pages in raster");
    fprintf(stderr, "DEBUG: rastertopksm wrote %u page(s)\n", pages);
    return 0;
}
