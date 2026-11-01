        const response = await fetch(
          "{{ url_for('room_send_message', room_code=room['room_code']) }}",
          { method: "POST", body: formData }
        );

        if (!response.ok) {
          chatStatus.textContent = "送信できませんでした。";
          return;
        }

        chatInput.value = "";
        chatStatus.textContent = "";
        await loadMessages();

      } catch (error) {
        chatStatus.textContent = "送信に失敗しました。";
      }
    });

    let loadingMessages = false;
    async function safeLoadMessages() {
      if (loadingMessages || document.hidden) return;
      loadingMessages = true;
      try { await loadMessages(); }
      finally { loadingMessages = false; }
    }

    safeLoadMessages();
    // Poll less often and never overlap requests. Pause while the tab is hidden.
    setInterval(safeLoadMessages, 10000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) safeLoadMessages();
    });
    </script>
    """

    return page(
        "ルーム",
        body,
        room=room,
        members=members,
        messages=messages,
        items=items,
    )
@app.route("/room/<room_code>/send", methods=["POST"])
def room_send_message(room_code):
    user = current_user()
    if user is None:
        abort(403)

    if not is_room_member(room_code, user["id"]):
        abort(403)

    message = request.form.get("message", "").strip()

    if not message or len(message) > 1000:
        return jsonify({"error": "メッセージは1〜1000文字です。"}), 400

    with connect_db() as conn:
        conn.execute(
            """INSERT INTO room_messages
               (room_code, user_id, message)
               VALUES (?, ?, ?)""",
            (room_code, user["id"], message),
        )

    return jsonify({"success": True})

@app.route("/room/<room_code>/messages")
def room_messages(room_code):
    user = current_user()
    if user is None:
        abort(403)

    if not is_room_member(room_code, user["id"]):
        abort(403)

    with connect_db() as conn:
        messages = conn.execute(
            """SELECT m.id, m.message, m.created_at, u.user_id
               FROM room_messages m
               JOIN users u ON u.id=m.user_id
               WHERE m.room_code=?
               ORDER BY m.id DESC
               LIMIT 100""",
            (room_code,),
        ).fetchall()

    return jsonify({
        "messages": [
            {
                "id": msg["id"],
                "message": msg["message"],
                "user_id": msg["user_id"],
                "created_at": str(msg["created_at"]),
            }
            for msg in reversed(messages)
        ]
    })
@app.errorhandler(413)
def too_large(_error):
    return "写真は10MB以下にしてください。", 413

# Create tables when the app starts.
init_db()

if __name__ == "__main__":
    # Local development only. For public hosting, use a production WSGI server.
    app.run(debug=False)
