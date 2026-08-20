# Odoo ERP Connector

Nanobot skill for Odoo 17 ERP integration. Control your business operations via natural language chat commands.

## Features

- **330+ operations** across core Odoo plus 20 AndersonTech custom modules
- **Smart actions** with fuzzy matching and auto-creation
- **Zero external dependencies** -- pure Python stdlib (`xmlrpc.client`)
- **Thread-safe** with retry logic and structured error handling

### Supported Modules

- Sales and CRM (quotations, orders, leads, opportunities)
- Purchasing (POs, vendors, receipts)
- Inventory (products, stock levels, alerts)
- Invoicing (customer invoices, payments)
- Projects and Tasks (management, timesheets)
- HR (employees, departments, expenses, leave)
- Fleet (vehicles, odometer, maintenance)
- Manufacturing (BOMs, production orders)
- Calendar (events, meetings)
- eCommerce (website orders, product publishing)

### AndersonTech Custom Modules

- Repairs (`atech_repair`) -- bench tickets, parts, labour, QC checklists, OEM shipments
- RMAs (`atech_rma`) and Warranty (`atech_warranty`)
- Helpdesk (`atech_helpdesk`) -- tickets, teams, escalations, eBay cases and disputes
- Messaging (`atech_messaging`) and Field Service (`atech_field_service`)
- Consignment (`atech_consignment`) and ITAD (`projects-custom`)
- Facebook Marketplace (`fb_marketplace_lister`) -- listings, renewal queue, AI content
- Inbound packages (`inbound_tracking`) -- carrier tracking, receipt confirmation
- Order status (`atech_order_status`) -- customer status links, quote signatures
- eBay messages (`odoo-ebay-messages`) -- buyer inbox, AI replies, order sync gaps
- Auction sourcing (`auction_scrapper_catalog`) -- lots, watchlists, bid approval
- Product photography (`product_photography`) -- studio sessions, AI digitization
- PC builds (`pc_configurator`) -- component specs, compatibility, quoting
- eBay listings, product drafts, and the to-do priority matrix

## Setup

### 1. Environment Variables (preferred)

```bash
export ODOO_URL="http://your-odoo-server:8069"
export ODOO_DB="your_database_name"
export ODOO_USERNAME="your_email@company.com"
export ODOO_API_KEY="your_odoo_api_key"
```

### 2. Config File (alternative)

Copy `config.json.template` to `config.json` and fill in your credentials:

```json
{
  "url": "http://your-odoo-server:8069",
  "db": "your_database_name",
  "username": "your_email@company.com",
  "api_key": "your_odoo_api_key"
}
```

### Getting an Odoo API Key

1. Log in to Odoo
2. Go to **Settings** > **Users & Companies** > **Users**
3. Open your user record
4. Scroll to **Access Tokens** and click **Generate**
5. Copy the key into your config or environment

## Usage Examples

```
"Create a quotation for Acme Corp with 10 Widgets at $50 each"
"Show me the sales pipeline"
"What's the stock level for Widget X?"
"Create task 'Fix login button' in Website Redesign"
"Submit expense report for $45.99"
```

See SKILL.md for the complete command reference.

## Requirements

- Python 3.10+
- Odoo 17
- No external Python dependencies

## Project Structure

```
skills/odoo/
  SKILL.md              # Nanobot skill definition
  config.json.template  # Configuration template
  odoo_skill/           # Python connector package
    client.py           # XML-RPC client
    config.py           # Configuration loader
    errors.py           # Custom exceptions
    smart_actions.py    # Smart action workflows
    models/             # 13 module operation classes
    sync/               # Change poller and webhook server
    utils/              # Helper utilities
```

## License

MIT -- see LICENSE file for details.
