<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>تصريح دخول يوتيوب بريميوم | إليفارا وبوكس أب</title>
<!-- مكتبة توليد الأكواد المدمجة -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/otpauth/9.1.2/otpauth.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap');
*{box-sizing:border-box;margin:0;padding:0;font-family:'Tajawal',sans-serif}
body{background:linear-gradient(135deg,#0f172a 0%,#020617 100%);color:#f8fafc;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.container{background:rgba(30,41,59,.6);backdrop-filter:blur(15px);border:1px solid rgba(255,255,255,.08);border-radius:24px;padding:40px 30px;width:100%;max-width:500px;box-shadow:0 25px 50px -12px rgba(0,0,0,.5);position:relative;overflow:hidden}
.brands-container{display:flex;justify-content:center;align-items:center;gap:25px;margin-bottom:25px;border-bottom:1px dashed rgba(255,255,255,.08);padding-bottom:25px}
.brand-item{display:flex;flex-direction:column;align-items:center;gap:8px}
.brand-logo{height:60px;object-fit:contain;border-radius:12px;filter:drop-shadow(0 5px 15px rgba(0,0,0,.3));background:#fff}
.brand-name{font-size:15px;font-weight:800;background:linear-gradient(135deg,#fff,#38bdf8);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}

/* الشاشات وحالات العرض */
#loader { text-align: center; color: #94a3b8; font-weight: bold; font-size: 18px; margin: 40px 0; }
#errorBox { display: none; text-align: center; border-top: 4px solid #ef4444; padding-top: 20px; }
#contentBox { display: none; animation: fadeIn .4s ease; }
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

/* رأس الصفحة */
.header{text-align:center;margin-bottom:30px}
.header-icon { font-size: 48px; color: #ef4444; margin-bottom: 15px; display: block; filter: drop-shadow(0 4px 10px rgba(239,68,68,0.4));}
.header h1{font-size:24px;font-weight:800;color:#fff;margin-bottom:12px}

/* صناديق البيانات */
.data-box { background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.05); padding: 15px 20px; border-radius: 16px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: center; }
.data-label { color: #94a3b8; font-size: 13px; margin-bottom: 5px; }
.data-value { font-family: monospace; font-size: 15px; color: #f8fafc; font-weight: bold; }
.copy-btn { background: #1e293b; color: #fff; border: none; padding: 10px; border-radius: 10px; cursor: pointer; transition: 0.3s; }
.copy-btn:hover { background: #334155; }

/* صندوق الكود */
.otp-container { background: rgba(220, 38, 38, 0.1); border: 1px solid rgba(153, 27, 27, 0.5); padding: 25px 20px; border-radius: 16px; text-align: center; margin-bottom: 20px; }
.otp-title { color: #fca5a5; font-size: 14px; font-weight: bold; margin-bottom: 15px; }
.code-display{font-size: clamp(34px,11vw,48px); font-weight:900; letter-spacing:6px; color:#fff; margin-bottom:20px; text-shadow:0 0 25px rgba(239,68,68,.4); font-family:monospace; direction:ltr; white-space:nowrap;}
.progress-bar{width:100%;height:6px;background:#450a0a;border-radius:10px;overflow:hidden;margin-bottom:10px}
.progress-fill{height:100%;background:#ef4444;width:100%;transition:width 1s linear}
.time-text{color:#94a3b8;font-size:12px}

/* التنبيهات */
.alert-box { background: rgba(234, 179, 8, 0.1); border: 1px solid rgba(161, 98, 7, 0.3); padding: 15px; border-radius: 12px; margin-bottom: 25px; }
.alert-box p { color: #fef08a; font-size: 13px; line-height: 1.6; margin-bottom: 5px; font-weight: bold; }
.alert-box ul { color: #d1d5db; font-size: 12px; margin-right: 20px; line-height: 1.7; }

/* قسم الدعم */
.support-section{text-align:center;padding-top:25px;border-top:1px dashed rgba(255,255,255,.1)}
.telegram-btn-support{display:inline-flex;align-items:center;justify-content:center;gap:10px;background:#229ED9;color:#fff;text-decoration:none;padding:12px 36px;border-radius:50px;font-size:16px;font-weight:700;box-shadow:0 8px 25px rgba(34,158,217,.4);transition:all .3s ease;direction:ltr}
.telegram-btn-support:hover{transform:translateY(-2px);box-shadow:0 12px 30px rgba(34,158,217,.6);background:#2bb3f3}

/* أيقونات مبسطة */
.icon-youtube::before { content: "▶"; font-size: 48px; color: #ef4444; }
.icon-copy::before { content: "📋"; font-size: 16px; }
.icon-check::before { content: "✅"; font-size: 16px; color: #10b981; }
.icon-error::before { content: "❌"; font-size: 48px; color: #ef4444; }
</style>
</head>
<body>

<div class="container">
  <!-- الهوية التجارية الخاصة بك -->
  <div class="brands-container">
    <div class="brand-item">
      <img src="../9a908a4b-668e-4fc8-8718-a8818453410e.jpeg" alt="Elevaraa" class="brand-logo" onerror="this.style.display='none'">
      <span class="brand-name">إليفارا</span>
    </div>
    <div class="brand-item">
      <img src="../e609dc1a-c899-4d45-8724-8bedeb033422.jpeg" alt="BoxUp" class="brand-logo" onerror="this.style.display='none'">
      <span class="brand-name">بوكس أب</span>
    </div>
  </div>

  <div id="loader">جاري استخراج البيانات المشفرة لمرة واحدة... ⏳</div>

  <div id="errorBox">
    <span class="icon-error"></span>
    <h2 style="font-size:20px; font-weight:bold; margin: 15px 0 10px;">الرابط غير صالح</h2>
    <p style="color: #94a3b8; font-size: 14px; line-height: 1.6;">عذراً، يبدو أن هذا الرابط قد تم فتحه مسبقاً لحماية الحساب، أو أنه غير صحيح.</p>
  </div>

  <div id="contentBox">
    <div class="header">
      <span class="icon-youtube"></span>
      <h1>تصريح دخول بريميوم</h1>
    </div>

    <div class="data-box">
      <div>
        <p class="data-label">البريد الإلكتروني</p>
        <p class="data-value" id="emailTxt"></p>
      </div>
      <button class="copy-btn" onclick="copyData('emailTxt', this)" title="نسخ البريد"><span class="icon-copy"></span></button>
    </div>

    <div class="data-box">
      <div>
        <p class="data-label">كلمة المرور</p>
        <p class="data-value" id="passTxt"></p>
      </div>
      <button class="copy-btn" onclick="copyData('passTxt', this)" title="نسخ كلمة المرور"><span class="icon-copy"></span></button>
    </div>

    <div class="otp-container">
      <p class="otp-title">🛡️ كود التحقق الآمن (2FA)</p>
      <div class="code-display" id="otpTxt">------</div>
      <div class="progress-bar"><div class="progress-fill" id="timeBar"></div></div>
      <p class="time-text" id="timeText">جاري تحديث الكود...</p>
    </div>

    <div class="alert-box">
      <p>⚠️ تحذير أمني هام:</p>
      <ul>
        <li>انسخ البيانات وسجل دخولك <b>الآن وفوراً</b>.</li>
        <li>لا تقم بتحديث هذه الصفحة وإلا ستحترق تذكرة الدخول نهائياً.</li>
        <li><b>يُمنع منعاً باتاً</b> تغيير كلمة المرور أو إعدادات الأمان للحساب.</li>
      </ul>
    </div>
    
    <div class="alert-box" style="background: rgba(56, 189, 248, 0.1); border-color: rgba(56, 189, 248, 0.3);">
      <p style="color: #38bdf8;">📌 طريقة الدخول:</p>
      <ul>
        <li>افتح تطبيق YouTube على جهازك.</li>
        <li>اختر "إضافة حساب" وأدخل الإيميل ثم كلمة المرور.</li>
        <li>سيطلب منك جوجل "رمز التحقق"، أدخل الكود الظاهر أعلاه المكون من 6 أرقام.</li>
      </ul>
    </div>

    <div class="support-section">
      <a href="https://t.me/elevaraa" target="_blank" rel="noopener" class="telegram-btn-support">
        <span>@elevaraa</span>
      </a>
    </div>
  </div>
</div>

<script>
    // الرابط المخفي للاتصال بـ Render API
    const API_BASE = "https://otp-system-gfa0.onrender.com";
    let otpSecret = "";

    async function init() {
        const urlParams = new URLSearchParams(window.location.search);
        const token = urlParams.get('token');
        
        if(!token) return showError();

        try {
            const res = await fetch(`${API_BASE}/api/yt/${token}`);
            if(!res.ok) throw new Error("Invalid Token or Burned");
            const data = await res.json();
            
            document.getElementById('emailTxt').innerText = data.email;
            document.getElementById('passTxt').innerText = data.password;
            otpSecret = data.secret;
            
            document.getElementById('loader').style.display = 'none';
            document.getElementById('contentBox').style.display = 'block';
            
            setInterval(updateTimer, 1000);
            updateTimer();
        } catch(e) {
            showError();
        }
    }

    function showError() {
        document.getElementById('loader').style.display = 'none';
        document.getElementById('errorBox').style.display = 'block';
    }

    function copyData(id, btn) { 
        navigator.clipboard.writeText(document.getElementById(id).innerText); 
        btn.innerHTML = '<span class="icon-check"></span>'; 
        setTimeout(() => btn.innerHTML = '<span class="icon-copy"></span>', 2000); 
    }

    function updateTimer() {
        if(!otpSecret) return;
        try {
            let totp = new OTPAuth.TOTP({ secret: OTPAuth.Secret.fromBase32(otpSecret) });
            document.getElementById('otpTxt').innerText = totp.generate();

            const timeLeft = 30 - (Math.floor(Date.now() / 1000) % 30);
            document.getElementById('timeBar').style.width = (timeLeft / 30 * 100) + '%';
            
            if(timeLeft <= 5) {
                document.getElementById('timeBar').style.background = '#f59e0b';
                document.getElementById('timeText').innerText = `يتغير الكود بعد ${timeLeft} ثواني!`;
                document.getElementById('timeText').style.color = '#f59e0b';
            } else {
                document.getElementById('timeBar').style.background = '#ef4444';
                document.getElementById('timeText').innerText = `صالح لمدة ${timeLeft} ثانية...`;
                document.getElementById('timeText').style.color = '#94a3b8';
            }
        } catch(e) { console.error("Error generating OTP", e); }
    }

    init();
</script>
</body>
</html>
