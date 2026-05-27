# NAV AI — R Shiny experimental view.
#
# This app is intentionally separate from the FastAPI service. It runs as
# its own process (default port 3838) and is iframed into the MCP shell's
# "Shiny" launcher tab. It reads from the same backend the rest of the
# views use, via the unauthenticated /dashboard/snapshot endpoint, and
# polls every few seconds. Nothing here is required for the rest of the
# demo to work — flip back to the launcher and the existing views are
# unchanged.
#
# Run from the repo root:
#   Rscript -e "shiny::runApp('shiny', port=3838, host='127.0.0.1')"
#
# Point it at a non-default backend with:
#   NAV_AI_URL=https://nav-mock-mcp.example.com \
#     Rscript -e "shiny::runApp('shiny', port=3838, host='127.0.0.1')"

library(shiny)
library(httr2)
library(jsonlite)
library(ggplot2)

`%||%` <- function(a, b) {
  if (is.null(a) || length(a) == 0 || (length(a) == 1 && is.na(a))) b else a
}

# Whichever service exposes /dashboard/snapshot — in this project that's
# the FRONTEND MCP service, since it aggregates from the backend pricing
# book internally. In local combined-mode dev both halves run on the same
# port, so 127.0.0.1:8000 covers both.
NAV_AI_URL <- Sys.getenv("NAV_AI_URL", "http://127.0.0.1:8000")
POLL_MS <- 3000

fetch_snapshot <- function() {
  tryCatch({
    resp <- httr2::request(paste0(NAV_AI_URL, "/dashboard/snapshot")) |>
      httr2::req_timeout(3) |>
      httr2::req_perform()
    httr2::resp_body_json(resp, simplifyVector = TRUE)
  }, error = function(e) list(error = conditionMessage(e)))
}

dark_css <- "
  body { background:#0d0d10; color:#e6e6ea; font-family:'Inter','Segoe UI',sans-serif; margin:0; padding:18px; }
  .nav-head { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:14px; }
  .nav-brand { font-family:'JetBrains Mono',monospace; font-size:13px; letter-spacing:.16em; color:#9aa0aa; }
  .nav-brand b { color:#e6e6ea; }
  .nav-rev { font-family:'JetBrains Mono',monospace; font-size:11px; color:#7a8087; }
  .tiles { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }
  .tile { background:#15161b; border:1px solid #24252c; border-radius:8px; padding:14px 16px; }
  .tile .k { font-family:'JetBrains Mono',monospace; font-size:10px; letter-spacing:.16em; color:#7a8087; text-transform:uppercase; }
  .tile .v { font-size:28px; font-weight:600; margin-top:6px; }
  .tile .d { font-size:11px; color:#9aa0aa; margin-top:4px; }
  .card { background:#15161b; border:1px solid #24252c; border-radius:8px; padding:16px; margin-bottom:16px; }
  .card h4 { margin:0 0 10px 0; font-size:12px; letter-spacing:.16em; color:#9aa0aa; text-transform:uppercase; font-weight:500; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; font-weight:500; color:#7a8087; font-size:10px; letter-spacing:.12em; text-transform:uppercase; border-bottom:1px solid #24252c; padding:8px 6px; }
  td { padding:8px 6px; border-bottom:1px solid #1d1e24; font-family:'JetBrains Mono',monospace; font-size:12px; }
  tr:last-child td { border-bottom:0; }
  .err { color:#ff6b6b; font-family:'JetBrains Mono',monospace; font-size:12px; }
"

ui <- fluidPage(
  tags$head(tags$style(HTML(dark_css))),
  div(class = "nav-head",
      div(class = "nav-brand", "NAV", tags$b("AI"), " · R SHINY"),
      div(class = "nav-rev", textOutput("rev_line", inline = TRUE))),
  uiOutput("error_box"),
  div(class = "tiles",
      div(class = "tile",
          div(class = "k", "Products"),
          div(class = "v", textOutput("t_products", inline = TRUE)),
          div(class = "d", textOutput("t_stock", inline = TRUE))),
      div(class = "tile",
          div(class = "k", "Pending"),
          div(class = "v", textOutput("t_pending", inline = TRUE)),
          div(class = "d", textOutput("t_pending_note", inline = TRUE))),
      div(class = "tile",
          div(class = "k", "Jobs"),
          div(class = "v", textOutput("t_jobs", inline = TRUE)),
          div(class = "d", textOutput("t_jobs_note", inline = TRUE))),
      div(class = "tile",
          div(class = "k", "Source"),
          div(class = "v", style = "font-size:14px;font-family:'JetBrains Mono',monospace",
              textOutput("t_backend", inline = TRUE)),
          div(class = "d", "polling every 3s"))
  ),
  div(class = "card",
      h4("Pending pricing changes — delta %"),
      plotOutput("delta_plot", height = "260px")),
  div(class = "card",
      h4("Recent pending"),
      uiOutput("pending_table")),
  div(class = "card",
      h4("Recent forecast jobs"),
      uiOutput("jobs_table"))
)

server <- function(input, output, session) {
  snap <- reactivePoll(
    POLL_MS, session,
    checkFunc = function() Sys.time(),
    valueFunc = fetch_snapshot
  )

  output$rev_line <- renderText({
    s <- snap()
    if (!is.null(s$error)) return(paste("backend:", s$error))
    paste0("snapshot ", format(Sys.time(), "%H:%M:%S"))
  })

  output$error_box <- renderUI({
    s <- snap()
    if (!is.null(s$error)) {
      div(class = "card", style = "border-color:#5a1f1f",
          div(class = "err", paste("Cannot reach backend:", s$error)),
          div(style = "font-size:11px;color:#7a8087;margin-top:6px",
              paste("Set NAV_AI_URL or start uvicorn on", NAV_AI_URL)))
    }
  })

  output$t_products <- renderText({
    s <- snap(); if (!is.null(s$error)) "—" else as.character(s$products %||% 0)
  })
  output$t_stock <- renderText({
    s <- snap(); if (!is.null(s$error)) ""
    else paste0(s$in_stock %||% 0, " in stock · ", s$out_of_stock %||% 0, " out")
  })
  output$t_pending <- renderText({
    s <- snap(); if (!is.null(s$error)) "—" else as.character(s$pending_pricing_changes %||% 0)
  })
  output$t_pending_note <- renderText({
    s <- snap(); if (!is.null(s$error)) ""
    else if ((s$pending_pricing_changes %||% 0) > 0) "awaiting review" else "queue clear"
  })
  output$t_jobs <- renderText({
    s <- snap(); if (!is.null(s$error)) "—" else as.character(s$jobs_total %||% 0)
  })
  output$t_jobs_note <- renderText({
    s <- snap(); if (!is.null(s$error)) ""
    else paste0(s$jobs_running %||% 0, " running · ", s$jobs_done %||% 0, " done")
  })
  output$t_backend <- renderText({ NAV_AI_URL })

  output$delta_plot <- renderPlot({
    s <- snap()
    pending <- s$recent_pending
    if (is.null(pending) || length(pending) == 0 || is.null(nrow(pending)) || nrow(pending) == 0) {
      df <- data.frame(product = character(), delta_pct = numeric())
    } else {
      df <- data.frame(
        product = pending$product,
        delta_pct = as.numeric(pending$delta_pct),
        ticket = pending$ticket,
        stringsAsFactors = FALSE
      )
      df <- df[!is.na(df$delta_pct), , drop = FALSE]
    }
    if (nrow(df) == 0) {
      ggplot() + annotate("text", x = 0, y = 0, label = "no pending pricing changes",
                          color = "#7a8087", family = "mono", size = 4) +
        theme_void() + theme(plot.background = element_rect(fill = "#15161b", colour = NA),
                             panel.background = element_rect(fill = "#15161b", colour = NA))
    } else {
      df$label <- paste0(df$product, " · ", df$ticket)
      df$fill <- ifelse(df$delta_pct >= 0, "#5fb878", "#e8746e")
      ggplot(df, aes(x = reorder(label, delta_pct), y = delta_pct, fill = fill)) +
        geom_col(width = 0.6) +
        geom_text(aes(label = sprintf("%+.1f%%", delta_pct)),
                  hjust = ifelse(df$delta_pct >= 0, -0.15, 1.15),
                  color = "#e6e6ea", size = 3.5, family = "mono") +
        scale_fill_identity() +
        coord_flip() +
        labs(x = NULL, y = "delta %") +
        theme_minimal(base_family = "mono") +
        theme(
          plot.background = element_rect(fill = "#15161b", colour = NA),
          panel.background = element_rect(fill = "#15161b", colour = NA),
          panel.grid.major.x = element_line(color = "#24252c"),
          panel.grid.major.y = element_blank(),
          panel.grid.minor = element_blank(),
          axis.text = element_text(color = "#9aa0aa"),
          axis.title = element_text(color = "#7a8087", size = 10),
          plot.margin = margin(6, 18, 6, 6)
        )
    }
  }, bg = "#15161b")

  output$pending_table <- renderUI({
    s <- snap()
    pending <- s$recent_pending
    if (is.null(pending) || length(pending) == 0 || is.null(nrow(pending)) || nrow(pending) == 0) {
      return(div(style = "color:#7a8087;font-style:italic;font-size:12px",
                 "No pending pricing changes."))
    }
    rows <- lapply(seq_len(nrow(pending)), function(i) {
      tags$tr(
        tags$td(format(as.POSIXct(pending$submitted_at[i], origin = "1970-01-01"), "%H:%M:%S")),
        tags$td(pending$ticket[i]),
        tags$td(pending$product[i]),
        tags$td(sprintf("%.2f → %.2f", pending$previous_price[i], pending$new_price[i])),
        tags$td(sprintf("%+.2f%%", pending$delta_pct[i])),
        tags$td(gsub("_", " ", pending$status[i]))
      )
    })
    tags$table(
      tags$thead(tags$tr(
        tags$th("time"), tags$th("ticket"), tags$th("product"),
        tags$th("price"), tags$th("delta"), tags$th("status")
      )),
      tags$tbody(rows)
    )
  })

  output$jobs_table <- renderUI({
    s <- snap()
    jobs <- s$recent_jobs
    if (is.null(jobs) || length(jobs) == 0 || is.null(nrow(jobs)) || nrow(jobs) == 0) {
      return(div(style = "color:#7a8087;font-style:italic;font-size:12px",
                 "No forecast jobs yet."))
    }
    rows <- lapply(seq_len(nrow(jobs)), function(i) {
      tags$tr(
        tags$td(format(as.POSIXct(jobs$started_at[i], origin = "1970-01-01"), "%H:%M:%S")),
        tags$td(jobs$id[i]),
        tags$td(jobs$region[i] %||% "—"),
        tags$td(jobs$status[i]),
        tags$td(if (!is.na(jobs$progress[i])) paste0(jobs$progress[i], "%") else "—")
      )
    })
    tags$table(
      tags$thead(tags$tr(
        tags$th("time"), tags$th("job"), tags$th("region"),
        tags$th("status"), tags$th("progress")
      )),
      tags$tbody(rows)
    )
  })
}

shinyApp(ui, server)
