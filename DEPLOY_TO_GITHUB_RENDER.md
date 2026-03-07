# Deploying Virtual Guard to GitHub + Render (Free)

## Step 1 — Push to GitHub (5 minutes)

1. Go to **github.com** → click **"New repository"**
2. Name it: `virtual-guard`
3. Set to **Private**, click **Create repository**
4. On your computer, open a terminal / command prompt and run:

```bash
# If you have git installed:
cd virtual_guard
git init
git add .
git commit -m "Virtual Guard v2 - Claude powered"
git remote add origin https://github.com/YOUR_USERNAME/virtual-guard.git
git push -u origin main
```

**Don't have git?** Upload the zip directly:
- On the GitHub repo page → click **"uploading an existing file"**
- Drag and drop all files from the zip
- Click **Commit changes**

---

## Step 2 — Deploy to Render.com (5 minutes, free)

1. Go to **render.com** → Sign up with your GitHub account
2. Click **"New +"** → **"Web Service"**
3. Select your `virtual-guard` repository
4. Render detects `render.yaml` automatically — click **Apply**
5. Fill in the **secret environment variables** (Render will prompt you):
   - `IZCLOUD_USERNAME` → `admin`
   - `IZCLOUD_PASSWORD` → `Inextech123`
   - `ANTHROPIC_API_KEY` → your key from console.anthropic.com
   - `TWILIO_ACCOUNT_SID` → from twilio.com/console
   - `TWILIO_AUTH_TOKEN` → from twilio.com/console
6. Click **Create Web Service**
7. Wait ~2 minutes for first deploy
8. Your URL will be: `https://virtual-guard.onrender.com`

**Test it:**
```
curl https://virtual-guard.onrender.com/health
```
Should return: `{"status": "ok", ...}`

---

## Step 3 — Configure Twilio Webhooks (3 minutes)

1. Go to **twilio.com/console** → Phone Numbers → **(220) 222-8122**
2. Under **Voice & Fax**:
   - "A Call Comes In" → Webhook → `https://virtual-guard.onrender.com/voice/incoming`
   - Method: **POST**
3. Under **Messaging**:
   - "A Message Comes In" → Webhook → `https://virtual-guard.onrender.com/sms/incoming`
   - Method: **POST**
4. Save

---

## You're Live!

Test by:
- Texting **(220) 222-8122** from a registered resident phone
- Calling the intercom and entering a PIN

---

## Free Tier Limitations (Render)

- App "sleeps" after 15 min of inactivity (takes ~30s to wake up)
- 750 hours/month free
- **For production**: upgrade to Render's $7/month plan — keeps app always on

---

*Inex Technology*
