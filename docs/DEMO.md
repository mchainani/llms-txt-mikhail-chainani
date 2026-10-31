# Demo: from a website to llms.txt

Turn a website into a downloadable Markdown index of its pages. Create a crawl
request, follow its run, and inspect the generated `llms.txt`.

These screenshots show the existing application with illustrative sample API
responses for [Profound’s documentation](https://docs.tryprofound.com/). The run
records and six-link output are illustrative fixtures, not the result of a live
crawl. The sample links are based on Profound’s public documentation index.

## 1. Create a request

![New request form with the Profound documentation URL, a 120-second timeout, and a 50-page limit](demo/01-create-request.jpg)

Open **Requests**, select **New request**, and enter the starting page's HTTP(S)
URL: **https://docs.tryprofound.com/**. In this example, the crawl has a
**120-second timeout** and a
**50-page limit**. The crawler follows links on the starting URL's hostname.

Leave **Cron expression** blank to run once immediately, then select
**Create request**. For recurring generation, enter a cron expression such as
`0 9 * * 1` for Mondays at 09:00 UTC; the first run starts at the next scheduled
time. If omitted, maximum pages defaults to 50 and timeout uses the server
default, normally 600 seconds.

## 2. Follow the run

![Request runs showing a successful example run with six links and zero retries](demo/02-run-history.jpg)

Creating a request opens **Request runs**. You can also reach it through
**View runs** on the Requests screen. This view shows each run's status,
retry count, number of links, job ID, and timestamps.

Pending runs refresh every five seconds. This sample shows **Success**, **6**
links, and **0** retries. Select **View file** to inspect its output. Use
**Start new run** when you want another run immediately, including for a
request that already has a recurring schedule.

## 3. Inspect and download llms.txt

![Generated file preview with a Download llms.txt button and six sample documentation links](demo/03-generated-file.jpg)

The **Generated file** screen displays the Markdown content and its size.
Review the page titles, URLs, and descriptions, then select
**Download llms.txt** to save the plain-text file.

The sample includes the Profound Knowledge Base, REST API introduction,
authentication, date ranges, response format, and changelog. In a real run, the output comes from the crawled pages'
metadata. To publish it, upload the downloaded file to your website so it is
available at `/llms.txt`.

## Run the application yourself

Follow the [backend setup](../backend/README.md) to start the API, Postgres, and
background workers. Then follow the [frontend setup](../frontend/README.md) to
start the interface. The screenshots above document the workflow; generating
files from your own website requires those services to be running.
