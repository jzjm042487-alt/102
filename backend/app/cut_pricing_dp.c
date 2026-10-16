/* Exact unbounded knapsack for cut-pattern pricing.
 *
 * Item i consumes weights[i] and scores values[i]. Capacity is inclusive.
 * mode 0 keeps every positive load up to cap.
 * mode 1 keeps loads <= stock, plus the single exact-fill load cap
 * (WITH_REMAINDER: trailing kerf is waived only when the bar is filled).
 *
 * Returns 0. Writes one optimal item-count vector and its value.
 * An empty pattern has value 0.
 */

#include <stdlib.h>
#include <string.h>

int price_knapsack(
    int n,
    const int *weights,
    const double *values,
    int cap,
    int mode,
    int stock,
    int *counts,
    double *out_value
) {
    int c;
    int i;
    double *dp;
    int *choice;
    int *prev;
    int best_c;
    double best;
    int guard;

    if (out_value == NULL || counts == NULL || n < 0 || cap < 0) {
        return 1;
    }
    memset(counts, 0, (size_t)n * sizeof(int));
    *out_value = 0.0;
    if (n == 0 || cap == 0) {
        return 0;
    }
    dp = (double *)malloc(((size_t)cap + 1) * sizeof(double));
    choice = (int *)malloc(((size_t)cap + 1) * sizeof(int));
    prev = (int *)malloc(((size_t)cap + 1) * sizeof(int));
    if (dp == NULL || choice == NULL || prev == NULL) {
        free(dp);
        free(choice);
        free(prev);
        return 2;
    }
    for (c = 0; c <= cap; c++) {
        dp[c] = -1.0e300;
        choice[c] = -1;
        prev[c] = -1;
    }
    dp[0] = 0.0;
    for (i = 0; i < n; i++) {
        int w = weights[i];
        double v = values[i];
        if (w <= 0 || v <= 0.0 || w > cap) {
            continue;
        }
        for (c = w; c <= cap; c++) {
            double cand = dp[c - w] + v;
            if (cand > dp[c] + 1.0e-12) {
                dp[c] = cand;
                choice[c] = i;
                prev[c] = c - w;
            }
        }
    }
    best_c = -1;
    best = 0.0;
    for (c = 1; c <= cap; c++) {
        if (mode == 1 && !(c <= stock || c == cap)) {
            continue;
        }
        if (dp[c] > best + 1.0e-12) {
            best = dp[c];
            best_c = c;
        }
    }
    c = best_c;
    guard = 0;
    while (c > 0 && guard <= cap) {
        int item = choice[c];
        if (item < 0 || item >= n) {
            break;
        }
        counts[item] += 1;
        c = prev[c];
        guard += 1;
    }
    *out_value = best;
    free(dp);
    free(choice);
    free(prev);
    return 0;
}
