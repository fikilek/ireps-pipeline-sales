# Understanding iREPS Conlog Sales

## A simple guide to Atomic Sales, Monthly Sales, Meter Master, and Sales All Meters

Conlog sales data is stored in different layers because each layer serves a different purpose.

The easiest way to understand it is this:

> **Atomic Sales shows every individual purchase.  
> Monthly Sales summarizes those purchases.  
> Meter Master tells us which meters exist and how they are linked.  
> Sales All Meters gives one complete sales-awareness profile for every known meter — including meters that bought nothing.**

---

## 1. Atomic Sales — Every Individual Purchase

**Firestore collection:**

```text
conlog_sales_atomic
```

Atomic Sales is the most detailed sales layer.

One atomic document represents one prepaid electricity purchase.

### Example

```text
Meter:      04085348348
Amount:     R200.00
Date:       2026-06-14
Time:       10:35
Provider:   Conlog
```

A single meter may therefore have many atomic documents.

### Atomic Sales helps us answer:

- What exact transactions took place?
- What time did each purchase happen?
- How much was each individual transaction?
- Which meter made the purchase?
- Can we audit a specific sale?

### Main purpose

Atomic Sales is the detailed **source of truth** for prepaid vending transactions.

---

## 2. Monthly Meter Sales — One Meter for One Month

**Firestore collection:**

```text
conlog_sales_monthly
```

Monthly Meter Sales combines all atomic transactions for one meter during one month.

Instead of reading every individual purchase, iREPS can read one monthly summary document.

### Example

```text
Meter:             04085348348
Month:             2026-06
Number of buys:    8
Total purchases:   R1,450.00
First purchase:    2026-06-02
Last purchase:     2026-06-27
```

### Monthly Meter Sales helps us answer:

- How much did this meter buy during the month?
- How many purchases did the meter make?
- When was its first purchase?
- When was its last purchase?
- Which monthly spending group does it belong to?

### Typical document identity

```text
ZA7423__04085348348__2026-06
```

This means:

```text
LM/workbase + meter number + month
```

### Main purpose

Monthly Meter Sales provides a fast monthly sales view for each meter.

---

## 3. Monthly LM Sales — One Municipality for One Month

**Firestore collection:**

```text
conlog_sales_monthly_lm
```

Monthly LM Sales combines the sales of all meters in one municipality or workbase for one month.

### Example

```text
LM/workbase:        ZA7423
Month:              2026-06
Meters purchasing:  15,400
Transactions:       128,000
Total revenue:      R24,500,000.00
```

### Monthly LM Sales helps us answer:

- What was the municipality's total prepaid revenue for the month?
- How many meters purchased electricity?
- How many transactions took place?
- Is revenue increasing or decreasing from month to month?
- What should be shown in top-level dashboard KPI cards?

### Typical document identity

```text
ZA7423__2026-06
```

There is normally one document per LM/workbase per month.

### Main purpose

Monthly LM Sales supports high-level municipal revenue reporting and dashboards.

---

## 4. Monthly LM Sales Groups — Meters Grouped by Spending Level

**Firestore collection:**

```text
conlog_sales_monthly_lm_groups
```

This layer groups meters according to how much they purchased during a month.

### Approved sales groups

```text
GR1: Below R100.00
GR2: R100.00 to R299.99
GR3: R300.00 to R499.99
GR4: R500.00 to R999.99
GR5: R1,000.00 and above
```

### Example

```text
LM/workbase:      ZA7423
Month:            2026-06
Sales group:      GR3
Meters:           2,800
Transactions:     6,450
Total sales:      R1,120,000.00
```

### Monthly LM Sales Groups helps us answer:

- How many meters are low purchasers?
- How many meters buy more than R1,000 per month?
- Which sales groups contain the most meters?
- Which groups contribute the most revenue?
- Which low-purchase groups may require investigation?

### Typical document identity

```text
ZA7423__2026-06__GR3
```

### Main purpose

Monthly LM Sales Groups supports sales segmentation, revenue analysis, and revenue-protection investigation.

---

## 5. Meter Master — The Meter Identity Bridge

**Firestore collection:**

```text
meter_master
```

Meter Master is not a sales-total collection.

It is the trusted identity bridge that tells iREPS which meters exist and how they connect to customer, sales, and iREPS asset records.

### Example

```text
Meter number:      04085348348
Customer number:   100503967
Account number:    100503967
Sales ID:          04085348348
Sales provider:    conlog
AST ID:            TRN_MINST_...
```

### Meter Master helps us answer:

- Does this meter exist in the governed meter register?
- What is its normalized meter number?
- Which customer and account belong to it?
- What is its Conlog sales reference?
- Is it linked to an iREPS AST record?

### Important point

Meter Master may contain meters from several sources:

```text
Monthly sales
Customer Details
90 Days No Purchase Report
iREPS AST references
```

This means Meter Master may include meters that have no recent monthly purchases.

### Main purpose

Meter Master provides one trusted identity record per known meter.

---

## 6. Sales All Meters — One Sales Profile for Every Known Meter

**Firestore collection:**

```text
sales-all-meters
```

Sales All Meters creates one ready-to-use sales profile for every meter in Meter Master.

It begins with the complete Meter Master and then adds the monthly sales history for each meter.

### Example

```text
Meter:                    04085348348
Customer:                 100503967
Account:                  100503967

September 2025 sales:     R500.00
October 2025 sales:       R600.00
November 2025 sales:      R0.00
December 2025 sales:      R750.00
January 2026 sales:       R800.00
February 2026 sales:      R700.00
March 2026 sales:         R900.00
April 2026 sales:         R850.00
May 2026 sales:           R950.00
June 2026 sales:          R900.00

Total sales:              R6,950.00
Last purchase:            2026-06-27
Days since last purchase: 17
```

### Why it is called “Sales All Meters”

It does not include only meters that purchased electricity.

It starts from Meter Master, so it can include:

```text
Meters with sales
Meters with zero sales
Meters found only in Customer Details
Meters found only in the 90 Days No Purchase Report
Meters linked to an iREPS AST
Meters not yet linked to an iREPS AST
```

### Example of a zero-sales meter

```text
Meter:                    01234567890
September 2025 sales:     R0.00
October 2025 sales:       R0.00
...
June 2026 sales:          R0.00
Total sales:              R0.00
Last purchase:            blank
Days since last purchase: blank
```

### Sales All Meters helps us identify:

- meters that have stopped purchasing;
- meters with very low purchases;
- meters with no sales history;
- possible meter bypassing or tampering;
- possible illegal connections;
- meters known to the municipality but missing from recent vending;
- sales meters that are not yet linked to an iREPS AST;
- meters that may need investigation or TRN creation.

### Main purpose

Sales All Meters gives iREPS one complete sales-awareness profile for every known meter.

---

## 7. How the Sales Layers Relate

### Sales aggregation flow

```text
Atomic Sales
    ↓
All individual transactions for each meter
    ↓
Monthly Meter Sales
    ↓
One summary per meter per month
    ↓
Monthly LM Sales
    ↓
One summary per LM/workbase per month
    ↓
Monthly LM Sales Groups
    ↓
LM monthly totals divided into spending groups
```

### Meter-awareness flow

```text
Monthly sales meters
Customer Details
90 Days No Purchase Report
iREPS AST references
        ↓
    Meter Master
        ↓
Combine each known meter with monthly sales history
        ↓
 Sales All Meters
```

---

## 8. A Simple Supermarket Analogy

Imagine a supermarket.

### Atomic Sales

```text
Every till receipt
```

### Monthly Meter Sales

```text
One customer's monthly shopping statement
```

### Monthly LM Sales

```text
The supermarket's total monthly turnover
```

### Monthly LM Sales Groups

```text
Customers grouped according to how much they spent
```

### Meter Master

```text
The full customer register
```

### Sales All Meters

```text
One dashboard profile for every registered customer,
including customers who bought nothing
```

---

## 9. The Most Important Difference

> **Monthly Sales tells us what purchasing meters did during a specific month.**

> **Sales All Meters gives us one complete sales-awareness profile for every known meter, including meters that purchased nothing.**

---

## 10. Summary Table

| Layer | Main question answered |
|---|---|
| Atomic Sales | What exact purchase happened? |
| Monthly Meter Sales | What did this meter buy during this month? |
| Monthly LM Sales | What did the whole municipality sell during this month? |
| Monthly LM Sales Groups | How are meters distributed across spending groups? |
| Meter Master | Which meters exist, and how are they linked? |
| Sales All Meters | What is the full sales profile of every known meter? |

---

## 11. Official Simple Definition

> **Atomic Sales records every individual prepaid purchase. Monthly Sales summarizes those purchases. Meter Master identifies every known meter. Sales All Meters combines Meter Master with monthly sales history to create one complete sales-awareness profile for every known meter — including meters with no purchases.**
