package com.openrubi.lucimatools

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.nio.charset.StandardCharsets
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** 使用 Android Keystore 保护由 Python 后端持久化的门户 Token。 */
object TokenVault {
    private const val KEY_ALIAS = "lucimatools_portal_token_v1"
    private const val PREFIX = "keystore-v1:"
    private const val IV_LENGTH = 12

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }

        val generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES,
            "AndroidKeyStore",
        )
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    @JvmStatic
    fun encrypt(plaintext: String, account: String): String {
        require(plaintext.isNotEmpty()) { "Token 不能为空" }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        cipher.updateAAD(account.toByteArray(StandardCharsets.UTF_8))
        val iv = cipher.iv
        require(iv.size == IV_LENGTH) { "不支持的 GCM IV 长度" }
        val encrypted = cipher.doFinal(plaintext.toByteArray(StandardCharsets.UTF_8))
        return PREFIX + Base64.encodeToString(iv + encrypted, Base64.NO_WRAP)
    }

    @JvmStatic
    fun decrypt(ciphertext: String, account: String): String {
        require(ciphertext.startsWith(PREFIX)) { "不支持的 Token 密文版本" }
        val packed = Base64.decode(ciphertext.removePrefix(PREFIX), Base64.NO_WRAP)
        require(packed.size > IV_LENGTH) { "Token 密文已损坏" }

        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(
            Cipher.DECRYPT_MODE,
            key(),
            GCMParameterSpec(128, packed.copyOfRange(0, IV_LENGTH)),
        )
        cipher.updateAAD(account.toByteArray(StandardCharsets.UTF_8))
        return String(
            cipher.doFinal(packed.copyOfRange(IV_LENGTH, packed.size)),
            StandardCharsets.UTF_8,
        )
    }
}
