/*
 * Palette mapping / dithering, ported from spr2color() in makeimage.cpp.
 *
 * rgb:      width*height*3 bytes (already rotated into buffer orientation,
 *           already quantised to RGB565 precision)
 * palette:  palette_len*3 bytes
 * out:      width*height palette indices
 * dither:   0 nearest, 1 Burkes error diffusion, 2 ordered pattern
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

typedef struct {
    int32_t r, g, b;
} Error;

static uint32_t color_distance(const uint8_t *c1, const uint8_t *c2, const Error *e)
{
    int r_diff = c1[0] + e->r - c2[0];
    int g_diff = c1[1] + e->g - c2[1];
    int b_diff = c1[2] + e->b - c2[2];
    if (abs(c1[0] - c1[1]) < 20 && abs(c1[2] - c1[1]) < 20) {
        if (abs(c2[0] - c2[1]) > 20 || abs(c2[2] - c2[1]) > 20) {
            return 4294967295u;
        }
    }
    double value = 3 * r_diff * r_diff + 5.47 * g_diff * g_diff + 1.53 * b_diff * b_diff;
    return (uint32_t)value;
}

static uint32_t rgb_value(const uint8_t *c)
{
    return ((uint32_t)c[0] << 16) | ((uint32_t)c[1] << 8) | c[2];
}

static void find_closest_colors(const uint8_t *pixel, const uint8_t *palette, int palette_len,
                                int *closest, int *second, double *d1, double *d2)
{
    static const Error zero = {0, 0, 0};
    *closest = -1;
    *second = -1;
    *d1 = INFINITY;
    *d2 = INFINITY;
    for (int i = 0; i < palette_len; i++) {
        double dist = (double)color_distance(pixel, palette + i * 3, &zero);
        if (dist < *d1) {
            *second = *closest;
            *d2 = *d1;
            *closest = i;
            *d1 = dist;
        } else if (dist < *d2) {
            *second = i;
            *d2 = dist;
        }
    }
    if (*closest != -1 && *second != -1) {
        if (rgb_value(palette + *second * 3) > rgb_value(palette + *closest * 3)) {
            int t = *closest;
            *closest = *second;
            *second = t;
            double td = *d1;
            *d1 = *d2;
            *d2 = td;
        }
    }
}

int oepl_dither(int width, int height, const uint8_t *rgb, const uint8_t *palette, int palette_len,
                int num_colors, int dither, uint8_t *out)
{
    if (num_colors > palette_len) {
        num_colors = palette_len;
    }
    Error *err_old = calloc(width + 4, sizeof(Error));
    Error *err_new = calloc(width + 4, sizeof(Error));
    if (!err_old || !err_new) {
        free(err_old);
        free(err_new);
        return -1;
    }
    for (int y = 0; y < height; y++) {
        memset(err_new, 0, (width + 4) * sizeof(Error));
        for (int x = 0; x < width; x++) {
            const uint8_t *color = rgb + (y * width + x) * 3;
            int best = 0;
            if (dither == 2) {
                int c1, c2;
                double d1, d2;
                find_closest_colors(color, palette, palette_len, &c1, &c2, &d1, &d2);
                double total = d1 + d2;
                double weight = total ? d1 / total : 0.0;
                if (weight <= 0.03) {
                    best = c1;
                } else if (weight < 0.30) {
                    best = (y % 2 && ((y / 2 + x) % 2)) ? c2 : c1;
                } else if (weight < 0.70) {
                    best = ((x + y) % 2) ? c2 : c1;
                } else if (weight < 0.97) {
                    best = ((y % 2 && ((y / 2 + x) % 2)) % 2) ? c1 : c2;
                } else {
                    best = c2;
                }
            } else {
                uint32_t best_distance = color_distance(color, palette, &err_old[x]);
                for (int i = 1; i < num_colors && best_distance > 0; i++) {
                    uint32_t distance = color_distance(color, palette + i * 3, &err_old[x]);
                    if (distance < best_distance) {
                        best_distance = distance;
                        best = i;
                    }
                }
            }
            out[y * width + x] = (uint8_t)best;

            if (dither == 1) {
                const uint8_t *chosen = palette + best * 3;
                int er = color[0] + err_old[x].r - chosen[0];
                int eg = color[1] + err_old[x].g - chosen[1];
                int eb = color[2] + err_old[x].b - chosen[2];
                int largest = abs(er);
                if (abs(eg) > largest) largest = abs(eg);
                if (abs(eb) > largest) largest = abs(eb);
                if (largest > 255) {
                    float scale = 255.0f / largest;
                    er = (int)(er * scale);
                    eg = (int)(eg * scale);
                    eb = (int)(eb * scale);
                }
                err_new[x].r += er / 4;
                err_new[x].g += eg / 4;
                err_new[x].b += eb / 4;
                if (x > 0) {
                    err_new[x - 1].r += er / 8;
                    err_new[x - 1].g += eg / 8;
                    err_new[x - 1].b += eb / 8;
                }
                if (x > 1) {
                    err_new[x - 2].r += er / 16;
                    err_new[x - 2].g += eg / 16;
                    err_new[x - 2].b += eb / 16;
                }
                err_new[x + 1].r += er / 8;
                err_new[x + 1].g += eg / 8;
                err_new[x + 1].b += eb / 8;
                err_old[x + 1].r += er / 4;
                err_old[x + 1].g += eg / 4;
                err_old[x + 1].b += eb / 4;
                err_new[x + 2].r += er / 16;
                err_new[x + 2].g += eg / 16;
                err_new[x + 2].b += eb / 16;
                err_old[x + 2].r += er / 8;
                err_old[x + 2].g += eg / 8;
                err_old[x + 2].b += eb / 8;
            }
        }
        memcpy(err_old, err_new, width * sizeof(Error));
    }
    free(err_old);
    free(err_new);
    return 0;
}
