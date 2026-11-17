# The demo shop database

A small appliance store: customers, products, orders and returns. It is the
backdrop for the WhatsApp support demo, so the agent has something true to
say when a real phone asks where its order is, and somewhere real to write
when it starts a return.

It lives as a second database, `shopdemo`, on the Postgres that compose
already runs. Nothing new to install.

## Create it, or reset it

```
./scripts/seed_demo_shop.sh
```

Drops and recreates everything, creates the `shop_agent` role with a fresh
password, and prints the connection string:

```
postgresql://shop_agent:...@localhost:5432/shopdemo
```

## Connect it to an agent

Options, Connectors, **Add database**, paste that string with the host
changed to `host.docker.internal` (the check runs inside a container, and
so does the agent, and that name reaches the compose Postgres from both):

```
postgresql://shop_agent:...@host.docker.internal:5432/shopdemo
```

Then attach the connection to the agent. Arya checks the database, finds
the shop's tables, and the card says the order tools will use it. Preview
shows them as live. No restart, no `.env` line: it is a per-agent setting
like any connector. A hosted database (Supabase, below) needs no host
change at all.

Run the script again whenever you want the shop back to its starting state.
Pass `SHOP_AGENT_PASSWORD=...` to keep the password already pasted on
Options, or paste the new string into the connection.

Without a shop-shaped database attached, the order desk keeps answering
from its built-in fake data, exactly as before this database existed.
`ARYA_SHOP_DSN` in `.env` still works as a worker-wide fallback for
installs without the connector, and is not needed otherwise.

## Who is in it

The first three customers are phones registered as Meta test recipients,
so a real handset is recognised by number. Only the first has messaged the
number so far; the other two are placeholders. Replace them in `seed.sql`
with the real registered numbers (digits only, country code first, no plus)
before a class, then reseed.

| Customer | Phone | What their orders let you show |
|---|---|---|
| Usama | 923358370304 | Two washers, both delivered inside their window, so the agent has to ask which one. A dishwasher on its way and a microwave still packing. This is the return story. |
| Amina | placeholder | A return already open and approved, so "what happened to my return" has an answer on day one. Plus a delivered kettle. |
| Bilal | placeholder | A fridge delivered 45 days ago with a 30 day window, so "too late to return" is a real answer the agent must give. And a cancelled order. |
| Sara, Daniel, Mei, Jonas | dummies | Never message. They make the tables look like a shop. |

Eight products across laundry, kitchen, small appliances and cleaning, with
30 day return windows on large items and 14 on small ones.

Dates are relative to the moment you seed, so the story does not age.

## What the agent's role can do

`shop_agent` can read every table and insert into `returns`. Nothing else.
It cannot change an order, a customer or a product, and it cannot update or
delete a return. Whatever the model asks for, the database accepts exactly
one kind of write. That is the point worth showing a class: the guard is in
Postgres, not in the prompt.

One open return per order is enforced by a unique index as well as by the
desk, so a second request for the same order fails at the database too.

## The policy document

`policy.md` is the store's returns, refunds and delivery policy, written to
agree with the seed: 30 days on large appliances and vacuums, 14 on small
appliances and microwaves, orders still packing are cancelled rather than
returned, one open return per order, the four return stages. Upload it in
the agent's Knowledge section. The template already says policy comes only
from store documents, so with it uploaded the agent quotes these terms, and
without it the agent says it does not have that detail. Orders never come
from the document; the desk is the only source for those.

## Moving it to Supabase

Supabase is Postgres. Create the same `shop_agent` role there, run
`seed.sql` against it (from the schema section onward; the database already
exists and Supabase does not allow creating one), and paste its connection
string on Options with `?sslmode=require` on the end. Nothing in Arya
changes: the check finds the same four tables and the order tools use it.

## Product pictures

Products carry two optional links. `image_url` is what WhatsApp shows,
fetched by Meta directly, so it must be public: the seed uses Unsplash
photos. `fallback_image_url` is a copy you host, tried when the first
cannot be fetched; the placeholders in `images/` are for that, uploaded to
a public Supabase Storage bucket named by SKU, then written with one
UPDATE per product (the pattern is at the end of the products section in
`seed.sql`). On WhatsApp the two-washer question then arrives as two photo
bubbles above the list, and a single order looked up comes with its
picture. A shop seeded before these columns existed still works, without
pictures.

## Running it for a class

`DEMO.md` is the run-through: the day-before checklist, who the personas
are, the message script with expected replies, what to put on the big
screen, what goes wrong, and the reset between groups.

## Looking inside

```
docker compose exec postgres psql -U arya -d shopdemo
```

```
select c.first_name, o.order_ref, o.status from orders o join customers c on c.id = o.customer_id order by c.id;
```

```
select return_ref, reason, note, status, created_at from returns;
```
