/*
 * ctypes wrapper around the Group5 encoder shipped with the ESP firmware.
 *
 * The encoder itself (g5enc.inl / Group5.h, BitBank Software) is included
 * from ESP32_AP-Flasher/src/g5 so that both access point implementations
 * compress with byte-identical code.  Build with `make` in this directory.
 */
#include <stdint.h>
#include <string.h>

#include "../../ESP32_AP-Flasher/src/g5/Group5.h"
#include "../../ESP32_AP-Flasher/src/g5/g5enc.inl"

/*
 * Compress a 1 bpp MSB-first bitmap.
 *
 * width/height: dimensions in pixels (rows are width/8 bytes, like the ESP)
 * in:           pixel data
 * out:          output buffer of out_size bytes
 * Returns the number of bytes written, or a negative G5 error code.
 */
int oepl_g5_encode(int width, int height, const uint8_t *in, uint8_t *out, int out_size)
{
    G5ENCIMAGE enc;
    int rc = g5_encode_init(&enc, width, height, out, out_size);
    const uint8_t *row = in;
    for (int y = 0; y < height && rc == G5_SUCCESS; y++) {
        rc = g5_encode_encodeLine(&enc, (uint8_t *)row);
        row += (width / 8);
    }
    if (rc != G5_ENCODE_COMPLETE) {
        return -rc;
    }
    return g5_encode_getOutSize(&enc);
}
