# HealthData Insight - public-health visualizations and statistics in base R.
#
# Usage: Rscript --vanilla analytics_plots.R <input_dir> <output_dir>
#
# Inputs (written by healthdata_insight.r_charts): patients.csv, conditions.csv,
# observations.csv. These are the de-identified analytical extracts - R never
# sees uploaded resources or PHI.
#
# Outputs in <output_dir>:
#   age_gender.png, condition_prevalence.png, observation_distributions.png
#   condition_prevalence_ci.csv   prevalence with exact (Clopper-Pearson) 95% CIs
#   observation_mean_ci.csv       mean with t-based 95% CIs per observation type
#   manifest.csv                  one row per output: name, title, file, kind, note
#
# Base R only (no CRAN packages), so it runs on any standard R installation.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) stop("usage: Rscript analytics_plots.R <input_dir> <output_dir>")
in_dir <- args[[1]]
out_dir <- args[[2]]
dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)

read_input <- function(name) {
  path <- file.path(in_dir, name)
  if (!file.exists(path) || file.info(path)$size == 0) return(data.frame())
  utils::read.csv(path, stringsAsFactors = FALSE, na.strings = c("", "NA"), encoding = "UTF-8")
}
patients <- read_input("patients.csv")
conditions <- read_input("conditions.csv")
observations <- read_input("observations.csv")

# Palette: the app's validated categorical slots and chart chrome.
INK <- "#52514e"
MUTED <- "#898781"
GRID <- "#e1e0d9"
SURFACE <- "#fcfcfb"
SERIES <- c("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
GENDER_COLORS <- c(female = SERIES[1], male = SERIES[2], other = SERIES[3], unknown = SERIES[4])
AGE_ORDER <- c("0-17", "18-34", "35-49", "50-64", "65-79", "80+")

manifest <- data.frame(name = character(), title = character(), file = character(),
                       kind = character(), note = character(), stringsAsFactors = FALSE)
record <- function(name, title, file, kind, note = "") {
  manifest[nrow(manifest) + 1, ] <<- list(name, title, file, kind, note)
}

open_png <- function(file, width = 1600, height = 1000) {
  png(file.path(out_dir, file), width = width, height = height, res = 200, bg = SURFACE)
  par(family = "sans", col.axis = MUTED, col.lab = INK, col.main = INK, fg = MUTED,
      mar = c(4, 4, 3, 1) + 0.1, las = 1, cex.axis = 0.8, cex.lab = 0.85, cex.main = 1, font.main = 1)
}

safely <- function(name, title, fn) {
  tryCatch(fn(), error = function(e) {
    while (dev.cur() > 1) dev.off()
    record(name, title, "", "error", conditionMessage(e))
  })
}

integer_ticks <- function(max_value) {
  ticks <- unique(round(pretty(c(0, max_value))))
  ticks[ticks >= 0]
}

# --- 1. Patients by age group and gender -------------------------------------------------
plot_age_gender <- function() {
  title <- "Patients by age group and gender"
  p <- if (nrow(patients)) patients[!is.na(patients$age_group), ] else patients
  if (nrow(p) == 0) return(record("age_gender", title, "", "skipped", "No patients with a known age."))
  genders <- names(GENDER_COLORS)[names(GENDER_COLORS) %in% p$gender]
  counts <- table(factor(p$gender, levels = genders), factor(p$age_group, levels = AGE_ORDER))
  ticks <- integer_ticks(max(counts))
  ylim <- c(0, max(ticks, max(counts)) * 1.12)

  open_png("age_gender.png", width = 1150, height = 900)  # shown at half width: smaller canvas, larger text
  barplot(counts, beside = TRUE, col = NA, border = NA, axes = FALSE, axisnames = FALSE, ylim = ylim,
          space = c(0.12, 0.9))
  abline(h = ticks, col = GRID, lwd = 0.8)
  barplot(counts, beside = TRUE, col = GENDER_COLORS[genders], border = SURFACE, lwd = 1.5, axes = FALSE,
          ylim = ylim, space = c(0.12, 0.9), add = TRUE)
  axis(2, at = ticks, col = NA, col.ticks = NA)
  title(xlab = "Age group", ylab = "Patients")
  legend("topright", legend = genders, fill = GENDER_COLORS[genders], border = NA, bty = "n",
         horiz = TRUE, cex = 0.75, text.col = INK)
  title(main = title, adj = 0)
  dev.off()
  record("age_gender", title, "age_gender.png", "plot")
}

# --- 2. Condition prevalence with exact 95% confidence intervals ------------------------------
plot_prevalence <- function() {
  title <- "Condition prevalence with exact 95% confidence intervals"
  total <- nrow(patients)
  linked <- if (nrow(conditions)) conditions[!is.na(conditions$patient_key), ] else conditions
  if (total == 0 || nrow(linked) == 0) {
    return(record("condition_prevalence", title, "", "skipped", "No conditions linked to patients."))
  }
  per_condition <- tapply(linked$patient_key, linked$condition, function(x) length(unique(x)))
  per_condition <- pmin(per_condition, total)
  ci <- t(vapply(per_condition, function(k) stats::binom.test(k, total)$conf.int, numeric(2)))
  result <- data.frame(condition = names(per_condition), patients = as.integer(per_condition), total = total,
                       prevalence_pct = 100 * as.numeric(per_condition) / total,
                       ci_low_pct = 100 * ci[, 1], ci_high_pct = 100 * ci[, 2], stringsAsFactors = FALSE)
  result <- result[order(-result$patients, result$condition), ]
  utils::write.csv(result, file.path(out_dir, "condition_prevalence_ci.csv"), row.names = FALSE)
  record("condition_prevalence_ci", "Condition prevalence (exact 95% CI)", "condition_prevalence_ci.csv", "table")

  top <- utils::head(result, 10)
  top <- top[rev(seq_len(nrow(top))), ]  # largest at the top
  y <- seq_len(nrow(top))
  labels <- ifelse(nchar(top$condition) > 38, paste0(substr(top$condition, 1, 36), "..."), top$condition)
  xmax <- min(100, ceiling(max(top$ci_high_pct) / 10) * 10)

  open_png("condition_prevalence.png", width = 1150, height = max(900, 420 + 110 * nrow(top)))
  par(mar = c(4, max(nchar(labels)) * 0.42 + 1, 3, 1.5))
  plot(NA, xlim = c(0, xmax), ylim = c(0.5, nrow(top) + 0.5), axes = FALSE,
       xlab = "Patients with the condition (%)", ylab = "")
  abline(v = pretty(c(0, xmax)), col = GRID, lwd = 0.8)
  segments(top$ci_low_pct, y, top$ci_high_pct, y, col = SERIES[1], lwd = 2)
  points(top$prevalence_pct, y, pch = 21, bg = SERIES[1], col = SURFACE, cex = 1.5, lwd = 2)
  axis(1, at = pretty(c(0, xmax)), col = NA, col.ticks = NA)
  axis(2, at = y, labels = labels, col = NA, col.ticks = NA, col.axis = INK)
  title(main = "Condition prevalence", adj = 0)
  mtext(sprintf("n = %d patients; exact 95%% CI", total), side = 3, adj = 0,
        line = 0.2, cex = 0.65, col = MUTED)
  dev.off()
  record("condition_prevalence", title, "condition_prevalence.png", "plot")
}

# --- 3. Distribution of numeric observation values (small multiples) -------------------------
plot_observations <- function() {
  title <- "Distribution of observation values"
  numeric_obs <- if (nrow(observations)) observations[!is.na(observations$value), ] else observations
  if (nrow(numeric_obs) == 0) {
    return(record("observation_distributions", title, "", "skipped", "No numeric observation values."))
  }
  numeric_obs$label <- ifelse(is.na(numeric_obs$unit), numeric_obs$observation,
                              paste0(numeric_obs$observation, " (", numeric_obs$unit, ")"))

  summary_rows <- lapply(split(numeric_obs, numeric_obs$label), function(d) {
    v <- d$value
    ci <- if (length(v) >= 2 && stats::sd(v) > 0) stats::t.test(v)$conf.int else c(NA, NA)
    data.frame(observation = d$observation[1], unit = d$unit[1], n = length(v), mean = mean(v),
               ci_low = ci[1], ci_high = ci[2], stringsAsFactors = FALSE)
  })
  summary_table <- do.call(rbind, summary_rows)
  summary_table <- summary_table[order(-summary_table$n), ]
  utils::write.csv(summary_table, file.path(out_dir, "observation_mean_ci.csv"), row.names = FALSE)
  record("observation_mean_ci", "Observation means (t-based 95% CI)", "observation_mean_ci.csv", "table")

  labels <- utils::head(names(sort(table(numeric_obs$label), decreasing = TRUE)), 6)
  columns <- if (length(labels) <= 4) min(2, length(labels)) else 3
  rows <- ceiling(length(labels) / columns)
  open_png("observation_distributions.png", width = 1800, height = 380 + 620 * rows)
  par(mfrow = c(rows, columns), mar = c(2.2, 3.5, 2.6, 1), oma = c(0, 0.8, 2.2, 0))
  set.seed(1)  # deterministic jitter
  for (label in labels) {
    v <- numeric_obs$value[numeric_obs$label == label]
    span <- range(v)
    if (diff(span) == 0) span <- span + c(-1, 1)
    plot(NA, xlim = c(0.4, 1.6), ylim = span, axes = FALSE, xlab = "", ylab = "")
    abline(h = pretty(span), col = GRID, lwd = 0.8)
    boxplot(v, at = 1, add = TRUE, axes = FALSE, outline = FALSE, boxwex = 0.55, col = "#cde2fb",
            border = SERIES[1], lwd = 1, medlwd = 2, whisklty = 1, staplelwd = 1)
    points(jitter(rep(1, length(v)), amount = 0.12), v, pch = 16, cex = 0.55,
           col = grDevices::adjustcolor(SERIES[1], alpha.f = 0.55))
    axis(2, at = pretty(span), col = NA, col.ticks = NA)
    short <- if (nchar(label) > 34) paste0(substr(label, 1, 32), "...") else label
    title(main = short, adj = 0, cex.main = 0.85)
    mtext(sprintf("n = %d", length(v)), side = 1, line = 0.6, cex = 0.6, col = MUTED)
  }
  mtext(title, outer = TRUE, adj = 0, line = 0.6, cex = 0.95, col = INK)
  dev.off()
  record("observation_distributions", title, "observation_distributions.png", "plot")
}

safely("age_gender", "Patients by age group and gender", plot_age_gender)
safely("condition_prevalence", "Condition prevalence", plot_prevalence)
safely("observation_distributions", "Distribution of observation values", plot_observations)

utils::write.csv(manifest, file.path(out_dir, "manifest.csv"), row.names = FALSE)
writeLines(R.version.string, file.path(out_dir, "r_version.txt"))
