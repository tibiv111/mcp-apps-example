# R Shiny app for the NAV AI mock MCP demo.
#
# rocker/r-ver ships R pre-built on Debian and points install2.r at P3M
# (Posit Public Package Manager) so CRAN packages install as Linux
# binaries instead of compiling from source — keeps the image build under
# a couple of minutes on Render's free tier.
FROM rocker/r-ver:4.4.1

RUN install2.r --error --skipinstalled \
      shiny \
      httr2 \
      jsonlite \
      ggplot2 \
 && rm -rf /tmp/downloaded_packages

WORKDIR /srv/shiny
COPY app.R ./app.R

# Render sets $PORT; default to 3838 for local `docker run` parity.
# host=0.0.0.0 so the bind is reachable from outside the container.
CMD ["sh", "-c", "R -e \"shiny::runApp('/srv/shiny', host='0.0.0.0', port=as.integer(Sys.getenv('PORT', '3838')))\""]
